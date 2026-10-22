"""Read-only MCP server (stdio) over the merlin index."""
from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone

import psycopg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config
from .embed import EmbedError, ModelMismatch
from .layout import load_layout
from .redact import check as check_redaction, redact
from .search import Hit, escape_like, host_history as _host_history, search as _search

STALE_MINUTES = 30
EXCERPT = 700
LAYOUT = load_layout()
check_redaction(LAYOUT)          # fail closed: no server without the configured secret hashes
GUIDE = f"""How to weigh results by doc type:
{LAYOUT.weighting_text()}
{LAYOUT.prefer_text()}
Say which document (path + date) an answer came from. [REDACTED:<name>] marks a live secret removed at index time,
[REDACTED] a secret-shaped string; never try to recover either.
An empty or weak result is NOT proof that something is absent or never happened: rephrase, try exact=true for
literal strings, or widen the filters before concluding a negative."""

ENT, ENTS = LAYOUT.entity_name, LAYOUT.entity_plural
LEGACY_HOST = ENT == "host"      # the original vocabulary: also serve host_history and search(host=) for older prompts

REPOS_DOC = (f"\nrepos: limit to these repos ({', '.join(n for n, _ in LAYOUT.repos)}); paths are <repo>/<path>."
             if LAYOUT.repos else "")

server = MCPServer("merlin", instructions=f"merlin: search over {LAYOUT.corpus}. The files are canonical; this "
                   "index is derived from them.\n" + GUIDE)
_conn: psycopg.Connection | None = None


def db() -> psycopg.Connection:
    """A live connection, reconnecting if the previous one dropped."""
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = config.connect()
        if config.DB_SCHEMA:
            config.require_index(_conn)      # a pinned but empty schema would silently serve public's index
    return _conn


def clear_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """Surface expected failures (model mismatch, backend down, bad dates) to the model as plain messages."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except (ModelMismatch, EmbedError, ValueError) as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return wrapper


def excerpt(body: str, query: str | None = None, size: int = EXCERPT) -> str:
    """A window of the body, centred on the first literal occurrence of the query when there is one."""
    pos = body.lower().find(query.lower()) if query else -1
    start = max(0, pos - size // 3) if pos > 0 else 0
    text = body[start:start + size].strip()
    return ("…" if start else "") + text + ("…" if start + size < len(body) else "")


def format_hit(n: int, h: Hit, query: str | None = None) -> str:
    """One result block: type, path, date, heading, entities, score, excerpt."""
    when = h.chunk_date or h.doc_date or "undated"
    more = f" (+{h.more} more in this doc)" if h.more else ""
    hosts = f" · {ENTS}: {', '.join(h.hosts)}" if h.hosts else ""
    score = f" · score {h.score:.4f}" if h.score else ""
    also = f"   also: {' · '.join('§ ' + a for a in h.also)}\n" if h.also else ""
    return (f"{n}. [{h.type}] {h.path} · {when}{score}{more}\n"
            f"   § {h.heading}{hosts}\n{also}" + re.sub(r"^", "   ", excerpt(h.body, query), flags=re.M))


def _fmt(hits: list[Hit], query: str | None, empty: str) -> str:
    """Results under the freshness verdict, so an empty or stale answer is never read without it."""
    body = "\n\n".join(format_hit(i, h, query) for i, h in enumerate(hits, 1)) or empty
    return freshness_line() + "\n\n" + body


SEARCH_DOC = f"""Hybrid search (semantic + full-text, fused by RRF) over {LAYOUT.corpus}.
Returns the best section per document (a newer section wins a near-tie; sections marked superseded/obsolete rank
lower) with type, path, date, heading, {ENTS}, score, an excerpt, and the other matching sections' headings.
types: filter by doc type ({', '.join(LAYOUT.weights)}). entity: a {ENT} name or alias{" (host= is the same)" if LEGACY_HOST else ""}. since/until: YYYY-MM-DD,
matched against the section's date. exact=true: literal + full-text only, for error strings, paths, IDs.
per_document=false: raw sections (several per document) instead of the best one per document.{REPOS_DOC}
{GUIDE}"""

HISTORY_DOC = f"""What has been seen and done on one {ENT}, NEWEST FIRST (chronological, not semantic): every section
that mentions it in {', '.join(LAYOUT.history_types)} docs, its own record included. Read this before working on a {ENT},
so recorded traps are read before they are re-hit. entity: a {ENT} name or alias. topic: keep only the sections that
best match it (still newest first).
{GUIDE}"""


def _search_impl(query: str, types: list[str] | None, entity: str | None, since: str | None, until: str | None,
                 k: int, exact: bool, repos: list[str] | None, per_document: bool) -> str:
    hits = _search(db(), query, types, entity, since, until, k=max(1, min(k, 30)), exact=exact, repos=repos,
                   per_document=per_document)
    return _fmt(hits, query if exact else None,
                "No results. Not proof of absence: rephrase, try exact=true, or drop filters.")


def _history_impl(entity: str, since: str | None, topic: str | None) -> str:
    hits = _host_history(db(), entity, since=since, topic=topic)
    return _fmt(hits, None, f"No history for {entity!r}. Check the name/alias with list_documents(types="
                            f"{list(LAYOUT.host_types)!r}).")


if LEGACY_HOST:
    @server.tool(description=SEARCH_DOC)
    @clear_errors
    def search(query: str, types: list[str] | None = None, entity: str | None = None, since: str | None = None,
               until: str | None = None, k: int = 8, exact: bool = False, repos: list[str] | None = None,
               per_document: bool = True, host: str | None = None) -> str:
        """Search tool (host= kept as an alias of entity=)."""
        return _search_impl(query, types, entity or host, since, until, k, exact, repos, per_document)
else:
    @server.tool(description=SEARCH_DOC)
    @clear_errors
    def search(query: str, types: list[str] | None = None, entity: str | None = None, since: str | None = None,
               until: str | None = None, k: int = 8, exact: bool = False, repos: list[str] | None = None,
               per_document: bool = True) -> str:
        """Search tool."""
        return _search_impl(query, types, entity, since, until, k, exact, repos, per_document)


@server.tool(description=HISTORY_DOC)
@clear_errors
def history(entity: str, since: str | None = None, topic: str | None = None) -> str:
    """Entity history tool."""
    return _history_impl(entity, since, topic)


if LEGACY_HOST:
    @server.tool(description="The same as history(entity=host): one host's sections, newest first. Kept for prompts "
                             "written before history() existed.")
    @clear_errors
    def host_history(host: str, since: str | None = None, topic: str | None = None) -> str:
        """Host history tool (alias of history)."""
        return _history_impl(host, since, topic)


@server.tool(description="The full text of one indexed document by its path as search shows it (secrets "
                         "redacted). Read the whole document before acting on a search excerpt. With several repos, "
                         "pass the full <repo>/<path>, or repo= and the path inside that repo.")
@clear_errors
def get_document(path: str, repo: str | None = None) -> str:
    """Return a document from its repo's HEAD, falling back to its indexed chunks."""
    path = LAYOUT.vpath(repo, path) if repo else path
    row = db().execute("SELECT type, doc_date FROM documents WHERE path = %s", (path,)).fetchone()
    if not row:
        return f"{path!r} is not indexed. Use list_documents() to see what is."
    try:
        name, rel = LAYOUT.split(path)
        text = config.git("show", f"HEAD:{rel}", repo=dict(LAYOUT.sources(config.REPO_DIR))[name])
    except (RuntimeError, ValueError, KeyError):
        text = "\n\n".join(b for (b,) in db().execute("SELECT body FROM chunks WHERE path = %s ORDER BY ord",
                                                       (path,)).fetchall())
    return f"[{row[0]}] {path} · {row[1] or 'undated'}\n\n{redact(text, LAYOUT)[0]}"


@server.tool(description="List indexed documents (path, type, date, title), newest first — e.g. every journal day "
                         "in a month, or all runbooks whose path contains \"wazuh\". Filters: types / repos (lists; "
                         "type / repo for one), since / until (YYYY-MM-DD, on the document date), path_contains "
                         "(case-insensitive substring), limit (default 50, max 500).")
@clear_errors
def list_documents(types: list[str] | None = None, repos: list[str] | None = None, since: str | None = None,
                   until: str | None = None, path_contains: str | None = None, limit: int = 50,
                   type: str | None = None, repo: str | None = None) -> str:
    """List documents."""
    types = list(types or []) + ([type] if type else [])
    repos = list(repos or []) + ([repo] if repo else [])
    rows = db().execute(
        "SELECT path, type, doc_date, coalesce(title, '') FROM documents "
        "WHERE (cardinality(%(t)s::text[]) = 0 OR type = ANY(%(t)s)) AND (cardinality(%(r)s::text[]) = 0 OR repo = ANY(%(r)s)) "
        "AND (%(s)s::date IS NULL OR doc_date >= %(s)s) AND (%(u)s::date IS NULL OR doc_date <= %(u)s) "
        "AND (%(p)s::text IS NULL OR path ILIKE '%%' || %(p)s || '%%') "
        "ORDER BY doc_date DESC NULLS LAST, path LIMIT %(n)s",
        {"t": types, "r": repos, "s": since, "u": until, "p": escape_like(path_contains) if path_contains else None,
         "n": max(1, min(int(limit), 500))}).fetchall()
    return freshness_line() + "\n\n" + ("\n".join(f"{p} · {t} · {d or 'undated'} · {title}" for p, t, d, title in rows)
                                         or "No documents.")


Head = tuple[str | None, datetime | None]          # (commit sha, commit time) of a repo's HEAD; (None, None) = unreadable


def repo_heads() -> dict[str, Head]:
    """Each repo's newest commit: the last-fetched upstream if tracked, else HEAD. Key "" for a single-repo corpus."""
    out: dict[str, Head] = {}
    for name, d in LAYOUT.sources(config.REPO_DIR):
        out[name] = (None, None)
        for ref in ("@{upstream}", "HEAD"):
            try:
                sha, ct = config.git("log", "-1", "--format=%H %ct", ref, repo=d).split()
                out[name] = (sha, datetime.fromtimestamp(int(ct), timezone.utc))
                break
            except (RuntimeError, ValueError):
                continue
    return out


def freshness(meta: dict[str, str], heads: dict[str, Head], now: datetime,
              stale_minutes: int = STALE_MINUTES) -> tuple[str, list[str]]:
    """(verdict line, detail lines). An unindexed commit is normal for a few minutes (ingest runs on a timer), so it
    only makes the index STALE once the commit is older than stale_minutes; younger ones read "catching up". Also
    STALE: never ingested, the last ingest failed or is older than stale_minutes, a repo never indexed, a HEAD that
    cannot be read. A verdict that cannot see its input must not read as fresh."""
    stale, catching_up, lines = [], [], []
    last = meta.get("last_ingest_at")
    if last:
        age = (now - datetime.fromisoformat(last)).total_seconds() / 60
        lines.append(f"last ingest: {datetime.fromisoformat(last).astimezone(config.local_tz()):%Y-%m-%d %H:%M %Z}"
                     f" ({age:.0f} min ago)")
        if age > stale_minutes:
            stale.append(f"last ingest {age:.0f} min ago (> {stale_minutes})")
    else:
        stale.append("never ingested")
    if meta.get("last_ingest_error"):
        stale.append(f"last ingest failed: {meta['last_ingest_error']}")
    indexed = json.loads(meta.get("heads") or "{}") or ({"": meta["head_sha"]} if meta.get("head_sha") else {})
    for name, (sha, when) in sorted(heads.items()):
        label, ih = name or "repo", indexed.get(name, "")
        lines.append(f"{label}: indexed {ih[:12] or '-'} · head {(sha or 'unreadable')[:12]}")
        if not sha:
            stale.append(f"{label} HEAD unreadable")
        elif not ih:
            stale.append(f"{label} never indexed")
        elif sha != ih:
            age = max(0, (now - when).total_seconds() / 60) if when else stale_minutes + 1
            if age > stale_minutes:
                stale.append(f"{label}: commit {sha[:7]} is {age:.0f} min old and not indexed (ingest broken?)")
            else:
                catching_up.append(f"{label} ({age:.0f} min)")
    if stale:
        verdict = "⚠ INDEX STALE: " + "; ".join(stale) + " — results may be missing recent changes."
    elif catching_up:
        verdict = "index fresh; catching up on commits from the last few minutes: " + ", ".join(catching_up)
    else:
        verdict = "index fresh (all repos at their HEAD)" if len(heads) > 1 else "index fresh (at HEAD)"
    return verdict, lines


def freshness_line() -> str:
    """The one-line verdict every result starts with."""
    return freshness(config.get_meta(db()), repo_heads(), datetime.now(timezone.utc))[0]


@server.tool(description="Index freshness: indexed vs repo HEAD per repo, last ingest time, model, counts. Shows "
                         "⚠ INDEX STALE when a commit has been unindexed for over 30 minutes, the last ingest is over "
                         "30 minutes old or failed, or a repo cannot be read; a few minutes behind is normal "
                         "(\"catching up\"). Every search result starts with the same verdict line. Check it first in a "
                         "session, and before trusting that something is absent.")
@clear_errors
def index_status() -> str:
    """Index status tool."""
    meta = config.get_meta(db())
    verdict, lines = freshness(meta, repo_heads(), datetime.now(timezone.utc))
    lines += [f"model: {meta.get('embed_model_id', '-')} · schema v{meta.get('schema_version', '-')}",
              f"counts: {meta.get('counts', '{}')}"]
    report = verdict + "\n" + "\n".join(lines)
    if not meta:
        return report + "\n(schema not created yet: run ingest)"
    by_type = db().execute("SELECT type, count(*) FROM documents GROUP BY type ORDER BY type").fetchall()
    return report + "\nby type: " + json.dumps(dict(by_type))


def main() -> None:
    """Run the server over stdio."""
    server.run("stdio")


if __name__ == "__main__":
    main()
