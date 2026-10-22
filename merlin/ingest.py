"""Incremental ingest: git HEAD -> redact -> chunk -> embed (reusing by text sha) -> Postgres.

One transaction per document, so a failure mid-run never leaves a half-indexed document. Embedding happens
before each document's transaction; if the backend is unreachable the run stops cleanly and records the error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from . import config, metrics
from .chunk import HostMatcher, chunk_document
from .embed import EmbedConfig, EmbedError, Embedder, ModelMismatch, check_model_id
from .layout import Layout, load_layout
from . import redact as redaction
from .redact import redact

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def sha256(text: str) -> str:
    """Hex sha256 of a string (the embedding-reuse key)."""
    return hashlib.sha256(text.encode()).hexdigest()


def apply_schema(conn: psycopg.Connection, dim: int) -> None:
    """Apply sql/NNN_*.sql files newer than meta.schema_version, substituting the embedding dimension.

    Refuses a schema whose tables are not a merlin index (a `documents` table without `meta`, or a `chunks`
    table without `path`): some other application's tables, or an older index. Writing there would leave this
    index's state in it and fail part-way; the shape check still holds after such a run has created `meta`."""
    schema, chunks_cols = conn.execute(
        "SELECT current_schema(), (SELECT array_agg(column_name::text) FROM information_schema.columns "
        " WHERE table_schema = current_schema() AND table_name = 'chunks')").fetchone()
    own = config.own_tables(conn)                      # this schema only: never judged through the search_path
    has_docs, has_meta = "documents" in own, "meta" in own
    foreign = (has_docs and not has_meta) or (chunks_cols is not None and "path" not in chunks_cols)
    if foreign:
        raise RuntimeError(f"refusing to index into schema {schema!r}: its documents/chunks tables are not a "
                           "merlin index (set DB_SCHEMA to a schema of its own)")
    current = int(config.get_meta(conn).get("schema_version", 0))
    for f in sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if int(f.name[:3]) > current:
            with conn.transaction():
                conn.execute(f.read_text().replace("{{EMBED_DIM}}", str(int(dim))))


def column_dim(conn: psycopg.Connection) -> int:
    """The declared dimension of chunks.embedding."""
    return conn.execute("SELECT atttypmod FROM pg_attribute "
                        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'").fetchone()[0]


def reset_embeddings(conn: psycopg.Connection, cfg: EmbedConfig) -> None:
    """Drop every chunk, resize the vector column and force all documents to re-ingest under cfg's model."""
    with conn.transaction():
        conn.execute("DELETE FROM chunks")
        conn.execute("UPDATE documents SET blob_sha = ''")
        conn.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw")
        conn.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({int(cfg.dim)})")
        conn.execute("CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")
        config.set_meta(conn, embed_model_id=cfg.model_id)


def guard_model(conn: psycopg.Connection, cfg: EmbedConfig, reembed_all: bool) -> None:
    """Record the model id on first ingest; refuse a mismatch unless --reembed-all."""
    stored = config.get_meta(conn).get("embed_model_id")
    if reembed_all or (not stored and column_dim(conn) != cfg.dim):
        reset_embeddings(conn, cfg)
    elif not stored:
        config.set_meta(conn, embed_model_id=cfg.model_id)
    else:
        check_model_id(stored, cfg)


def list_head(repo: str, layout: Layout, name: str = "") -> dict[str, str]:
    """{virtual path: blob sha} of every indexable file at the repo's HEAD."""
    out = {}
    for line in config.git("ls-tree", "-r", "-z", "HEAD", repo=repo).split("\0"):
        if line:
            meta, path = line.split("\t", 1)
            _, kind, sha = meta.split()
            vpath = layout.vpath(name, path)
            if kind == "blob" and layout.doc_type(vpath):
                out[vpath] = sha
    return out


def run(conn: psycopg.Connection, repo: str | None, cfg: EmbedConfig, pull: bool = False,
        reembed_all: bool = False, embedder: Embedder | None = None,
        layout: Layout | None = None) -> dict[str, int | str]:
    """Bring the index up to date with each repo's HEAD. Returns the counts it records in meta.

    `repo` is the single-repo corpus (REPO_DIR); it is ignored when the layout configures [[repos]]."""
    t0 = time.monotonic()
    layout = layout or load_layout()
    redaction.check(layout)                             # secret hashes configured but unreadable: refuse, first
    sources = layout.sources(repo or config.REPO_DIR)
    dirs = dict(sources)
    if pull:
        for _, d in sources:
            config.git("pull", "--ff-only", "-q", repo=d)
    apply_schema(conn, cfg.dim)
    guard_model(conn, cfg, reembed_all)
    embedder = embedder or Embedder(cfg, "document")
    embedder.check()                                    # fail before touching anything if unreachable
    heads = {name: config.git("rev-parse", "HEAD", repo=d).strip() for name, d in sources}
    # Every repo is listed before anything is written: a repo that fails to list raises here, so its documents
    # are never mistaken for deleted ones.
    tree: dict[str, str] = {}
    for name, d in sources:
        tree.update(list_head(d, layout, name))

    def read_blob(vpath: str, sha: str) -> str:
        return config.git("cat-file", "blob", sha, repo=dirs[layout.split(vpath)[0]])

    host_docs = {p: read_blob(p, s) for p, s in tree.items() if layout.doc_type(p) in layout.host_types}
    matcher = HostMatcher.from_host_docs(host_docs, layout.host_text_ignore, layout.entity_key, layout.entity_alias_fields)
    host_map = json.dumps(matcher.aliases, sort_keys=True)
    host_ignore = json.dumps(sorted(matcher.text_ignore))
    chunk_config = json.dumps({"max_tokens": layout.chunk_max_tokens})
    meta = config.get_meta(conn)
    known = dict(conn.execute("SELECT path, blob_sha FROM documents").fetchall())
    # host list or ignore list changed: re-tag everything (embeddings reused)
    # chunking settings changed: re-chunk everything too (an unchanged chunk text keeps its vector)
    rehost = (meta.get("host_map") != host_map or meta.get("host_text_ignore", "[]") != host_ignore
              or meta.get("chunk_config", json.dumps({"max_tokens": 1500})) != chunk_config)
    stats = dict(docs=len(tree), changed=0, unchanged=0, deleted=0, embedded=0, reused=0, redactions=0)
    cache: dict[str, str] = {}
    for path, sha in sorted(tree.items()):
        if known.get(path) == sha and not rehost:
            stats["unchanged"] += 1
            continue
        name, rel = layout.split(path)
        text, n = redact(read_blob(path, sha), layout)
        stats["redactions"] += n
        last = config.git("log", "-1", "--format=%cs", "HEAD", "--", rel, repo=dirs[name]).strip()
        doc = chunk_document(path, text, matcher, date.fromisoformat(last) if last else None, layout=layout)
        for w in doc.warnings:
            print(f"warning: {path}: {w}", file=sys.stderr)
        texts = [cfg.doc_prefix + c.text for c in doc.chunks]
        shas = [sha256(t) for t in texts]
        need = [s for s in set(shas) if s not in cache]
        if need:
            cache.update(conn.execute("SELECT DISTINCT ON (text_sha) text_sha, embedding::text FROM chunks "
                                      "WHERE text_sha = ANY(%s) AND embedding IS NOT NULL", (need,)).fetchall())
        missing = list(dict.fromkeys(t for t, s in zip(texts, shas) if s not in cache))
        stats["reused"] += len(shas) - len(missing)
        if missing:
            cache.update(zip(map(sha256, missing), embedder.embed(missing)))
            stats["embedded"] += len(missing)
        with conn.transaction():
            conn.execute("DELETE FROM documents WHERE path = %s", (path,))
            conn.execute("INSERT INTO documents (path, repo, type, title, doc_date, blob_sha) "
                         "VALUES (%s,%s,%s,%s,%s,%s)", (path, name, doc.type, doc.title, doc.doc_date, sha))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chunks (path, ord, heading, heading_path, body, hosts, chunk_date, text_sha, embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                    [(path, c.ord, c.heading, c.heading_path, c.body, c.hosts, c.chunk_date, s, cache[s])
                     for c, s in zip(doc.chunks, shas)])
        stats["changed"] += 1
    prev_heads = json.loads(meta.get("heads") or "{}")
    moved = json.loads(meta.get("repo_indexed_at") or "{}")   # when each repo's index last moved to a new HEAD
    now = time.time()
    moved = {n: (now if prev_heads.get(n) != h or n not in moved else moved[n]) for n, h in heads.items()}
    with conn.transaction():
        gone = [p for p in known if p not in tree]
        if gone:
            conn.execute("DELETE FROM documents WHERE path = ANY(%s)", (gone,))
        stats["deleted"] = len(gone)
        stats["chunks"] = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        config.set_meta(conn, last_ingest_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        head_sha=heads.get("", ""), heads=json.dumps(heads, sort_keys=True), host_map=host_map,
                        host_text_ignore=host_ignore, chunk_config=chunk_config, repo_indexed_at=json.dumps(moved, sort_keys=True),
                        counts=json.dumps(stats), last_ingest_error="")
    if os.environ.get("METRICS_FILE"):
        metrics.write(conn, os.environ["METRICS_FILE"], sources, heads, moved, now)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    stats["head"] = " ".join(f"{n}@{h[:10]}" if n else h[:10] for n, h in sorted(heads.items()))
    return stats


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m merlin.ingest [--pull] [--reembed-all]`."""
    ap = argparse.ArgumentParser(description="Index the corpus repo(s) into Postgres.")
    ap.add_argument("--pull", action="store_true", help="git pull --ff-only before indexing")
    ap.add_argument("--reembed-all", action="store_true", help="discard all vectors and rebuild with the configured model")
    ap.add_argument("--repo", default=config.REPO_DIR, help="the single-repo corpus (ignored when the layout has [[repos]])")
    args = ap.parse_args(argv)
    cfg = EmbedConfig.from_env()
    with config.connect(create_schema=True) as conn:
        try:
            s = run(conn, args.repo, cfg, pull=args.pull, reembed_all=args.reembed_all)
        except (EmbedError, ModelMismatch, RuntimeError, ValueError) as e:
            if config.get_meta(conn):
                config.set_meta(conn, last_ingest_error=f"{datetime.now(timezone.utc):%FT%TZ} {e}")
            print(f"ingest FAILED: {e}", file=sys.stderr)
            return 2
    print(f"ingest {s['head']} [{cfg.model_id}]: {s['docs']} docs ({s['changed']} changed, "
          f"{s['unchanged']} unchanged, {s['deleted']} deleted), {s['chunks']} chunks, {s['embedded']} embedded, "
          f"{s['reused']} reused, {s['redactions']} redactions, {s['seconds']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
