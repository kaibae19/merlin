"""Environment configuration, database connection, git and meta-table helpers."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://merlin@127.0.0.1:5432/merlin")  # PGPASSWORD via libpq
REPO_DIR = os.environ.get("REPO_DIR", "/repo")
DB_SCHEMA = os.environ.get("DB_SCHEMA", "")   # pin the index to this schema (search_path), whatever the role's default


INDEX_TABLES = ("documents", "chunks", "meta")


def connect(url: str | None = None, create_schema: bool = False, **kwargs: Any) -> psycopg.Connection:
    """Open an autocommit connection (use `with conn.transaction()` for atomic work). With DB_SCHEMA set, the
    connection's search_path is `<schema>, public`, which overrides any per-role default.

    Postgres silently skips a search_path schema that doesn't exist, so a missing DB_SCHEMA would make every
    unqualified name resolve in `public`: that's an error here, unless `create_schema` (ingest) creates it. Creating
    it needs CREATE on the database; a role without it fails loudly here (only when the schema is missing)."""
    pinned = bool(DB_SCHEMA) and "options" not in kwargs
    if pinned:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", DB_SCHEMA):
            raise ValueError(f"DB_SCHEMA must be a plain lower-case identifier, got {DB_SCHEMA!r}")
        kwargs["options"] = f"-c search_path={DB_SCHEMA},public"
    conn = psycopg.connect(url or DATABASE_URL, autocommit=True, **kwargs)
    if pinned and conn.execute("SELECT current_schema()").fetchone()[0] != DB_SCHEMA:
        if not create_schema:
            conn.close()
            raise RuntimeError(f"DB_SCHEMA {DB_SCHEMA!r} does not exist (names would resolve in public)")
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
    return conn


def own_tables(conn: psycopg.Connection) -> set[str]:
    """The merlin index tables that exist in the connection's own schema (current_schema()). Unqualified names
    fall through the search_path to `public`, so presence is never judged by to_regclass('<name>')."""
    return {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
        "AND table_name = ANY(%s)", (list(INDEX_TABLES),)).fetchall()}


def require_index(conn: psycopg.Connection) -> None:
    """Refuse to read when the connection's own schema holds no merlin index (it would read public's instead)."""
    missing = set(INDEX_TABLES) - own_tables(conn)
    if missing:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        raise RuntimeError(f"schema {schema!r} has no merlin index (missing {', '.join(sorted(missing))}); "
                           "run ingest with the same DB_SCHEMA first")


def git(*args: str, repo: str | None = None) -> str:
    """Run git against the repo. safe.directory=* because the container user rarely owns the checkout."""
    r = subprocess.run(["git", "-c", "safe.directory=*", "-C", repo or REPO_DIR, *args],
                       capture_output=True, text=True, check=False)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def get_meta(conn: psycopg.Connection) -> dict[str, str]:
    """All meta key/values ({} before the schema exists). Only the connection's own schema counts: an unqualified
    `meta` would otherwise be read from public when the pinned schema is still empty."""
    if "meta" not in own_tables(conn):
        return {}
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def set_meta(conn: psycopg.Connection, **values: Any) -> None:
    """Upsert meta key/values."""
    for k, v in values.items():
        conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (k, str(v)))


def local_tz() -> tzinfo:
    """The display timezone from TZ (an IANA name); UTC when unset. Never hardcoded."""
    name = os.environ.get("TZ", "").lstrip(":")
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc
