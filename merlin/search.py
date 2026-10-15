"""Hybrid search (pgvector + Postgres full-text, fused by reciprocal rank) and chronological host history."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import PurePosixPath

import psycopg

from .chunk import _as_date
from .embed import EmbedConfig, Embedder, check_model_id
from .layout import Layout, load_layout
from . import config

RRF_K = 60
NEAR_TIE = 0.10        # within a document, a newer section this close to the best one is shown instead
ALSO = 2               # other section headings named per document
_query_embedder: Embedder | None = None
_checked_model: set[int] = set()


@dataclass
class Hit:
    id: int
    path: str
    type: str
    title: str
    doc_date: date | None
    chunk_date: date | None
    heading: str
    hosts: list[str]
    body: str
    score: float = 0.0
    more: int = 0          # other matching chunks in the same document (per_document mode)
    also: list[str] = field(default_factory=list)   # headings of the next-best other sections (per_document mode)
    stale: bool = False    # the section marks itself superseded/obsolete (set by rerank)
    heading_path: list[str] = field(default_factory=list)   # [h1, h2, h3] of the chunk's section


def rrf(rankings: list[list[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal-rank fusion: score(id) = sum over rankings of 1 / (k + rank), rank starting at 1."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, i in enumerate(ranking, start=1):
            scores[i] = scores.get(i, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _date(v: str | date | None) -> date | None:
    if isinstance(v, date) or v is None:
        return v
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"dates must be YYYY-MM-DD, got {v!r}") from None


def build_filters(types: list[str] | None = None, host: str | None = None, since: str | date | None = None,
                  until: str | date | None = None, repos: list[str] | None = None) -> tuple[str, dict]:
    """SQL `AND ...` fragment + params for the type/host/date/repo filters (dates match the section's date)."""
    sql, params = "", {}
    if types:
        sql += " AND d.type = ANY(%(types)s)"
        params["types"] = list(types)
    if repos:
        sql += " AND d.repo = ANY(%(repos)s)"
        params["repos"] = list(repos)
    if host:
        sql += " AND %(host)s = ANY(c.hosts)"
        params["host"] = host
    if since:
        sql += " AND coalesce(c.chunk_date, d.doc_date) >= %(since)s"
        params["since"] = _date(since)
    if until:
        sql += " AND coalesce(c.chunk_date, d.doc_date) <= %(until)s"
        params["until"] = _date(until)
    return sql, params


FROM = " FROM chunks c JOIN documents d ON d.path = c.path"


def vector_sql(where: str) -> str:
    """Nearest chunks by cosine distance; re-sorted because relaxed_order iterative scans may be out of order."""
    return (f"WITH v AS MATERIALIZED (SELECT c.id, c.embedding <=> %(qv)s::vector AS dist{FROM} "
            f"WHERE c.embedding IS NOT NULL{where} ORDER BY dist LIMIT %(n)s) SELECT id FROM v ORDER BY dist")


def fts_sql(where: str) -> str:
    """Full-text matches ranked by ts_rank_cd (heading weighted A, body B)."""
    return (f"SELECT c.id{FROM}, websearch_to_tsquery('english', %(q)s) query WHERE c.tsv @@ query{where} "
            f"ORDER BY ts_rank_cd(c.tsv, query) DESC, c.id LIMIT %(n)s")


def ilike_sql(where: str) -> str:
    """Literal substring matches (error strings, IDs), newest first."""
    return (f"SELECT c.id{FROM} WHERE (c.body ILIKE %(like)s OR c.heading ILIKE %(like)s){where} "
            f"ORDER BY coalesce(c.chunk_date, d.doc_date) DESC NULLS LAST, c.id LIMIT %(n)s")


def escape_like(s: str) -> str:
    """Escape LIKE wildcards so the query matches literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def own_date(h: Hit) -> date | None:
    """The section's own date: one in its heading chain, else a chunk date that isn't just the document's date.
    An undated section (front matter, intro) inherits the doc date, which for host records is the last commit, so
    any edit would make it "newest" and let it beat the dated section that answers (2026-10-12)."""
    for x in reversed(h.heading_path[1:] or [h.heading]):
        if d := _as_date(x):
            return d
    return h.chunk_date if h.chunk_date != h.doc_date else None


def group_per_document(hits: list[Hit], near_tie: float = NEAR_TIE, also: int = ALSO) -> list[Hit]:
    """One hit per document (input best-first), in the order of each document's best score.

    The shown section is the best one, unless another section of the same document scores within `near_tie` of it
    and is unmarked where the best is marked stale, or, when the best is dated, has a newer own date (`own_date`; a later section usually supersedes an earlier one; undated sections never win or lose on recency). The others are counted in `more`, and the
    headings of the next `also` best are named so the reader knows which section to open."""
    docs: dict[str, list[Hit]] = {}
    for h in hits:
        docs.setdefault(h.path, []).append(h)
    out = []
    for group in docs.values():
        top = group[0]
        close = [h for h in group if h.score >= top.score * (1 - near_tie)]
        cands = [h for h in close if not h.stale] or close       # unmarked beats stale
        best = cands[0]
        # recency only between dated sections: an undated best (Facts, intro) stays; a dated best yields to a newer one
        dated = [h for h in cands if own_date(h)] if own_date(best) else []
        pick = max(dated, key=lambda h: (own_date(h), h.score)) if dated else best
        others = [h.heading for h in group if h is not pick and h.heading != pick.heading]
        out.append(replace(pick, score=top.score, more=len(group) - 1, also=list(dict.fromkeys(others))[:also]))
    return out


def ensure_model(conn: psycopg.Connection, cfg: EmbedConfig) -> None:
    """Refuse to query an index built with a different embedding model."""
    if id(conn) not in _checked_model:
        check_model_id(config.get_meta(conn).get("embed_model_id"), cfg)
        _checked_model.add(id(conn))


def embed_query(conn: psycopg.Connection, text: str) -> str:
    """Embed a query with the query backend (EMBED_URL_QUERY if set), after the model-identity check."""
    global _query_embedder
    cfg = EmbedConfig.from_env()
    ensure_model(conn, cfg)
    if _query_embedder is None:
        _query_embedder = Embedder(cfg, "query")
    return _query_embedder.embed_one(text)


def resolve_host(conn: psycopg.Connection, host: str | None) -> str | None:
    """Map an alias or IP to the canonical host name recorded at ingest."""
    if not host:
        return None
    aliases = json.loads(config.get_meta(conn).get("host_map") or "{}")
    return aliases.get(host.lower(), host)


def ranked_ids(conn: psycopg.Connection, query: str, where: str, params: dict, n: int,
               exact: bool = False, fusion: str = "hybrid") -> list[tuple[int, float]]:
    """Fuse the vector (or literal ILIKE, in exact mode) leg with the full-text leg by RRF. With fusion="vector"
    a semantic query uses the vector leg alone; exact mode always fuses ILIKE with full-text."""
    p = {**params, "q": query, "n": n}
    with conn.transaction():
        if exact:
            first = conn.execute(ilike_sql(where), {**p, "like": f"%{escape_like(query)}%"}).fetchall()
        else:
            conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            conn.execute(f"SET LOCAL hnsw.ef_search = {max(100, min(1000, n * 4))}")
            first = conn.execute(vector_sql(where), {**p, "qv": embed_query(conn, query)}).fetchall()
            if fusion == "vector":
                return rrf([[r[0] for r in first]])
        lexical = conn.execute(fts_sql(where), p).fetchall()
    return rrf([[r[0] for r in first], [r[0] for r in lexical]])


def fetch_hits(conn: psycopg.Connection, scored: list[tuple[int, float]]) -> list[Hit]:
    """Load chunk rows for (id, score) pairs, preserving order."""
    if not scored:
        return []
    rows = conn.execute("SELECT c.id, c.path, d.type, coalesce(d.title, ''), d.doc_date, c.chunk_date, c.heading, "
                        f"c.hosts, c.body, c.heading_path{FROM} WHERE c.id = ANY(%s)", ([i for i, _ in scored],)).fetchall()
    by_id = {r[0]: Hit(*r[:9], heading_path=list(r[9] or [])) for r in rows}
    return [replace(by_id[i], score=s) for i, s in scored if i in by_id]


def inherit_stale(hits: list[Hit], types: tuple[str, ...]) -> list[Hit]:
    """An overview restates the sections below it: a journal's day index still says what a since-superseded section
    said, and outranks it. For doc types in `types`, an unmarked chunk whose heading chain is a strict prefix of a
    stale-marked chunk's chain in the same document (both matched the query) counts as stale too. Siblings and the
    chunks of one split section are never ancestors of each other, so a current section next to a stale one keeps
    its score."""
    if not types:
        return hits
    stale_paths: dict[str, list[list[str]]] = {}
    for h in hits:
        if h.stale and h.type in types:
            stale_paths.setdefault(h.path, []).append(h.heading_path)
    under = lambda a, b: len(a) < len(b) and b[:len(a)] == a
    return [replace(h, stale=True) if not h.stale and h.type in types
            and any(under(h.heading_path, s) for s in stale_paths.get(h.path, ())) else h for h in hits]


def rerank(hits: list[Hit], layout: Layout, today: date | None = None) -> list[Hit]:
    """Re-score fused hits by doc-type boost and a small recency factor, best first (stable on ties).

    Fused RRF scores are nearly flat, so the canonical layers (host records, runbooks, lessons) would otherwise
    lose to the journal entry that produced them, or to an index that only points at them."""
    today = today or date.today()

    def adjusted(h: Hit) -> float:
        s = h.score * layout.boost.get(h.type, 1.0)
        if h.stale:
            s *= layout.stale_factor
        d = h.chunk_date or h.doc_date
        if layout.recency_weight and d:
            s *= 1 + layout.recency_weight * 0.5 ** (max(0, (today - d).days) / layout.recency_half_life)
        return s

    marked = inherit_stale([replace(h, stale=layout.is_stale(h.heading, h.body)) for h in hits], layout.stale_inherit_types)
    return sorted((replace(h, score=adjusted(h)) for h in marked), key=lambda h: -h.score)


def search(conn: psycopg.Connection, query: str, types: list[str] | None = None, host: str | None = None,
           since: str | None = None, until: str | None = None, k: int = 8, exact: bool = False,
           per_document: bool = True, layout: Layout | None = None, repos: list[str] | None = None) -> list[Hit]:
    """Hybrid search. `exact` = literal + full-text only (no embedding), for error strings and IDs.
    `repos` limits a multi-repo corpus to those [[repos]] names."""
    layout = layout or load_layout()
    where, params = build_filters(types, resolve_host(conn, host), since, until, repos)
    n = max(50, k * 8)
    hits = rerank(fetch_hits(conn, ranked_ids(conn, query, where, params, n, exact, layout.search_fusion)), layout)
    if per_document:
        hits = group_per_document(hits)
    return hits[:k]


def host_history(conn: psycopg.Connection, host: str, since: str | None = None, topic: str | None = None,
                 limit: int = 20, layout: Layout | None = None) -> list[Hit]:
    """A host's journal and host-record chunks, newest first. With `topic`, keep the `limit` best hybrid
    matches within that host's chunks, still returned in chronological order. Layout [history]: `exclude` drops
    matching paths (e.g. daily handoffs that restate other docs); `record_chunks` > 0 leads with the host's own
    record (current practice: the first N chunks, or with a topic the matching ones), then the dated history."""
    layout = layout or load_layout()
    host = resolve_host(conn, host)
    where, params = build_filters(list(layout.history_types), host, since)
    keep = lambda h: not any(p.match(h.path) for p in layout.history_exclude)
    own = lambda h: h.type in layout.host_types and PurePosixPath(h.path).stem == host
    fetch = limit * 3 if layout.history_exclude else limit          # room for the rows `exclude` drops
    if topic:
        hits = fetch_hits(conn, ranked_ids(conn, topic, where, params, max(50, limit * 4),
                                           fusion=layout.search_fusion)[:fetch])
    else:
        rows = conn.execute(f"SELECT c.id{FROM} WHERE true{where} ORDER BY coalesce(c.chunk_date, d.doc_date) "
                            "DESC NULLS LAST, c.path, c.ord LIMIT %(limit)s", {**params, "limit": fetch}).fetchall()
        hits = fetch_hits(conn, [(r[0], 0.0) for r in rows])
    hits = [h for h in hits if keep(h)]
    lead: list[Hit] = []
    if layout.history_record_chunks and host:
        if topic:
            lead = [h for h in hits if own(h)][:layout.history_record_chunks]
        else:
            rows = conn.execute(f"SELECT c.id{FROM} WHERE d.type = ANY(%(own_types)s) AND c.path ~ %(own_path)s{where} "
                                "ORDER BY c.ord LIMIT %(n)s", {**params, "own_types": list(layout.host_types),
                                "own_path": f"(^|/){re.escape(host)}\\.md$", "n": layout.history_record_chunks}).fetchall()
            lead = fetch_hits(conn, [(r[0], 0.0) for r in rows])
        hits = [h for h in hits if not own(h)]
    hits = sorted(hits, key=lambda h: (h.chunk_date or h.doc_date or date.min, h.path, -h.id), reverse=True)
    return (lead + hits)[:limit]
