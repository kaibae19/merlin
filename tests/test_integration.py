"""End-to-end against a real Postgres+pgvector (needs DATABASE_URL; runs in a throwaway schema).

A deterministic bag-of-words embedder stands in for the model so only the database is required;
`test_real_embedder` additionally needs EMBED_URL.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import psycopg
import psycopg.conninfo
import pytest

from conftest import CORPUS
from merlin import config, ingest, search
from merlin.embed import EmbedConfig, Embedder, ModelMismatch, to_literal
from merlin.layout import DEFAULT_TOML, Layout

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
CFG = EmbedConfig.from_env({})
TEST_SCHEMAS = ("merlin_test_", "merlin_v1_", "merlin_pin_", "merlin_fresh_")   # this suite's throwaway schemas


def indexed_schemas(c):
    """Schemas holding merlin index tables, other than this suite's throwaway ones."""
    rows = c.execute("SELECT DISTINCT table_schema FROM information_schema.tables WHERE table_name = ANY(%s)",
                     (list(config.INDEX_TABLES),)).fetchall()
    return sorted(s for (s,) in rows if not s.startswith(TEST_SCHEMAS))


@pytest.fixture(scope="module", autouse=True)
def refuse_indexed_database():
    """2026-10-09: run inside a deployed container, these tests read the live index through
    `search_path=<test schema>,public`. Refuse any database that already holds a merlin index."""
    with config.connect(options="-c search_path=public") as c:
        found, where = indexed_schemas(c), f"{c.info.host}/{c.info.dbname}"
    if found:
        pytest.exit(f"refusing integration tests: {where} already holds merlin index tables in {found}; "
                    "point DATABASE_URL at a throwaway database", returncode=3)


class FakeEmbedder(Embedder):
    def __init__(self, cfg=CFG, kind="document"):
        self.cfg, self.kind, self.calls = cfg, kind, 0

    def check(self):
        pass

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            v = [0.0] * self.cfg.dim
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.cfg.dim] += 1
            norm = math.sqrt(sum(x * x for x in v)) or 1
            out.append(to_literal([x / norm for x in v]))
        return out


def sh(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": "2030-02-01T00:00:00Z", "GIT_COMMITTER_DATE": "2030-02-01T00:00:00Z",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "corpus"
    shutil.copytree(CORPUS, r)
    sh(r, "init", "-q", "-b", "main")
    sh(r, "add", "-A")
    sh(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def conn(monkeypatch):
    schema = f"merlin_test_{uuid.uuid4().hex[:8]}"
    admin = config.connect()
    admin.execute(f"CREATE SCHEMA {schema}")
    c = config.connect(options=f"-c search_path={schema},public")
    for k in [k for k in os.environ if k.startswith("EMBED_")]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(search, "_query_embedder", FakeEmbedder(kind="query"))
    search._checked_model.clear()
    yield c
    c.close()
    admin.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def test_ingest_incremental(conn, repo):
    fake = FakeEmbedder()
    s = ingest.run(conn, str(repo), CFG, embedder=fake)
    assert (s["docs"], s["changed"], s["deleted"]) == (10, 10, 0) and s["embedded"] == fake.calls > 0
    assert config.get_meta(conn)["embed_model_id"] == CFG.model_id
    s = ingest.run(conn, str(repo), CFG, embedder=fake)
    assert (s["changed"], s["unchanged"], s["embedded"]) == (0, 10, 0)
    (repo / "planning/backlog.md").write_text("# Backlog\n- (2030-01-15) Grow beta's root volume.\n- new item\n")
    (repo / "skills/daily-check/SKILL.md").unlink()
    sh(repo, "commit", "-qam", "edit")
    s = ingest.run(conn, str(repo), CFG, embedder=fake)
    assert (s["changed"], s["deleted"], s["embedded"]) == (1, 1, 1)
    bodies = " ".join(b for (b,) in conn.execute("SELECT body FROM chunks").fetchall())
    assert "hunter2secret" not in bodies and "[REDACTED]" in bodies



def test_v2_to_v3_rechunks_without_reembedding(conn, repo):
    """Schema v3 adds chunks.heading_path: a v2 index re-chunks every document on the next ingest, but the chunk
    texts are unchanged, so no embedding is recomputed."""
    ingest.run(conn, str(repo), CFG, embedder=FakeEmbedder())
    conn.execute("ALTER TABLE chunks DROP COLUMN heading_path")
    config.set_meta(conn, schema_version="2")
    fake = FakeEmbedder()
    s = ingest.run(conn, str(repo), CFG, embedder=fake)
    assert (s["changed"], s["embedded"], fake.calls) == (10, 0, 0) and s["reused"] > 0
    assert config.get_meta(conn)["schema_version"] == "3"
    assert conn.execute("SELECT heading_path FROM chunks WHERE heading = 'Verify'").fetchone()[0] == ["Backup", "Verify"]
    assert [h.heading_path for h in search.search(conn, "backup verify", k=1)] == [["Backup", "Verify"]]

def test_search_and_history(conn, repo):
    ingest.run(conn, str(repo), CFG, embedder=FakeEmbedder())
    hits = search.search(conn, "scrub checksum errors cable")
    assert {h.path for h in hits[:3]} & {"hosts/alpha.md", "journal/2030/01/2030-01-14.md", "memory/scrub-cables.md"}
    assert len({h.path for h in hits}) == len(hits)                       # one per document
    ex = search.search(conn, "Failed to restore initial working directory", exact=True)
    assert ex[0].path == "journal/2030/01/2030-01-15.md" and ex[0].heading.endswith("Mount error on 192.0.2.20")
    assert all("beta" in h.hosts for h in search.search(conn, "disk", host="beta.example.internal"))
    assert {h.type for h in search.search(conn, "disk", types=["runbook"])} <= {"runbook"}
    assert all((h.chunk_date or h.doc_date).isoformat() >= "2030-01-16"
               for h in search.search(conn, "disk", since="2030-01-16"))
    hist = search.host_history(conn, "beta")
    dates = [h.chunk_date or h.doc_date for h in hist]
    assert dates == sorted(dates, reverse=True)
    journal = [h for h in hist if h.type == "journal"]                  # undated host sections carry the git date
    assert journal[0].heading == "beta after the prune" and journal[-1].chunk_date.isoformat() == "2030-01-15"
    assert {h.type for h in hist} == {"journal", "host"}
    assert all("alpha" in h.hosts for h in search.host_history(conn, "nas"))   # alias resolves
    topical = search.host_history(conn, "alpha", topic="scrub", limit=2)
    assert len(topical) == 2


def test_model_guard(conn, repo):
    ingest.run(conn, str(repo), CFG, embedder=FakeEmbedder())
    other = EmbedConfig.from_env({"EMBED_MODEL": "other/model"})
    with pytest.raises(ModelMismatch, match="--reembed-all"):
        ingest.run(conn, str(repo), other, embedder=FakeEmbedder(other))
    s = ingest.run(conn, str(repo), other, embedder=FakeEmbedder(other), reembed_all=True)
    assert s["changed"] == 10 and config.get_meta(conn)["embed_model_id"] == other.model_id
    with pytest.raises(ModelMismatch):                                     # queries refuse too (env = defaults)
        search.search(conn, "disk")
    small = EmbedConfig.from_env({"EMBED_MODEL": "tiny", "EMBED_DIM": "8"})
    ingest.run(conn, str(repo), small, embedder=FakeEmbedder(small), reembed_all=True)
    assert ingest.column_dim(conn) == 8


@pytest.mark.skipif(not os.environ.get("EMBED_URL"), reason="EMBED_URL not set")
def test_real_embedder():
    cfg = EmbedConfig.from_env()
    a, b, c = (Embedder(cfg, "document").embed([cfg.doc_prefix + t]) for t in
               ("the disk is full", "the storage volume ran out of space", "a recipe for lemon cake"))
    vec = lambda s: [float(x) for x in s[0].strip("[]").split(",")]
    dot = lambda x, y: sum(i * j for i, j in zip(vec(x), vec(y)))
    assert len(vec(a)) == cfg.dim and dot(a, b) > dot(a, c)


MULTI_LAYOUT = """
exclude = ["**/_template.md"]
layers = [
  { glob = "notes/journal/**",       type = "journal" },
  { glob = "infra/hosts/*.md",       type = "host" },
  { glob = "infra/runbooks/**",      type = "runbook" },
  { glob = "notes/memory/MEMORY.md", type = "memory-index" },
  { glob = "notes/memory/*.md",      type = "memory" },
  { glob = "notes/planning/**",      type = "planning" },
  { glob = "notes/skills/**",        type = "skill" },
  { glob = "notes/*.md",             type = "doc" },
]
[[repos]]
name = "notes"
dir = "%s"
[[repos]]
name = "infra"
dir = "%s"
"""


@pytest.fixture
def two_repos(tmp_path):
    """The fixture corpus split in two: `infra` (hosts, runbooks) as a BARE mirror, `notes` (the rest) as a checkout."""
    work, notes = tmp_path / "infra-work", tmp_path / "notes"
    shutil.copytree(CORPUS, notes)
    work.mkdir()
    for d in ("hosts", "runbooks"):
        shutil.move(notes / d, work / d)
    for r in (work, notes):
        sh(r, "init", "-q", "-b", "main")
        sh(r, "add", "-A")
        sh(r, "commit", "-q", "-m", "init")
    bare = tmp_path / "infra.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return Layout.from_toml(MULTI_LAYOUT % (notes, bare)), notes, work, bare


def test_multi_repo(conn, two_repos, monkeypatch):
    lay, notes, work, bare = two_repos
    s = ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    assert (s["docs"], s["changed"]) == (10, 10) and "infra@" in s["head"] and "notes@" in s["head"]
    rows = dict(conn.execute("SELECT path, repo FROM documents").fetchall())
    assert rows["infra/hosts/alpha.md"] == "infra" and rows["notes/journal/2030/01/2030-01-14.md"] == "notes"
    assert all(p.startswith(r + "/") for p, r in rows.items())
    heads = json.loads(config.get_meta(conn)["heads"])
    assert heads == {"infra": config.git("rev-parse", "HEAD", repo=str(bare)).strip(),
                     "notes": config.git("rev-parse", "HEAD", repo=str(notes)).strip()}
    # host records in one repo tag mentions in the other
    tagged = conn.execute("SELECT DISTINCT unnest(hosts) FROM chunks c JOIN documents d ON d.path = c.path "
                          "WHERE d.repo = 'notes'").fetchall()
    assert {"alpha", "beta"} <= {h for (h,) in tagged}
    hits = search.search(conn, "disk", repos=["infra"], layout=lay)
    assert hits and all(h.path.startswith("infra/") for h in hits)
    monkeypatch.setattr("merlin.layout.load_layout", lambda *a, **k: lay)
    monkeypatch.setattr(search, "load_layout", lambda *a, **k: lay)
    hist = search.host_history(conn, "beta")
    assert {h.path.split("/")[0] for h in hist} == {"notes", "infra"}       # journal + host record, both repos
    # incremental: a commit in one repo re-indexes only its file
    (notes / "planning/backlog.md").write_text("# Backlog\n- new item\n")
    sh(notes, "commit", "-qam", "edit")
    s = ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    assert (s["changed"], s["unchanged"], s["deleted"]) == (1, 9, 0)
    # a repo that cannot be read stops the run before anything is deleted
    count = "SELECT repo, count(*) FROM documents GROUP BY repo ORDER BY repo"
    before = conn.execute(count).fetchall()
    assert dict(before)["infra"] == 3                                       # alpha, beta, the backup runbook
    broken = Layout.from_toml(MULTI_LAYOUT % (notes, work.parent / "missing.git"))
    with pytest.raises(RuntimeError):
        ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=broken)
    assert conn.execute(count).fetchall() == before


def test_mcp_freshness_end_to_end(conn, two_repos, monkeypatch):
    from merlin import mcp_server
    lay, notes, work, bare = two_repos
    ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    monkeypatch.setattr(mcp_server, "LAYOUT", lay)
    monkeypatch.setattr(mcp_server, "_conn", conn)
    monkeypatch.setattr(search, "load_layout", lambda *a, **k: lay)
    status = mcp_server.index_status()
    assert status.startswith("index fresh (all repos at their HEAD)") and "infra: indexed" in status
    out = mcp_server.search("disk")
    assert out.startswith("index fresh (all repos at their HEAD)\n\n1. [")
    assert mcp_server.list_documents(repo="infra").startswith("index fresh")
    (notes / "planning/backlog.md").write_text("# Backlog\n- fresh item\n")
    subprocess.run(["git", "-C", str(notes), "commit", "-qam", "now"], check=True, capture_output=True)
    assert "catching up on commits from the last few minutes: notes (0 min)" in mcp_server.search("disk")
    (notes / "planning/backlog.md").write_text("# Backlog\n- old item\n")
    old = {**os.environ, "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z"}
    subprocess.run(["git", "-C", str(notes), "commit", "-qam", "old"], check=True, capture_output=True, env=old)
    stale = mcp_server.search("disk")
    assert stale.startswith("⚠ INDEX STALE: notes: commit ") and "not indexed (ingest broken?)" in stale
    ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    assert mcp_server.index_status().startswith("index fresh (all repos at their HEAD)")


def test_metrics_file(conn, two_repos, tmp_path, monkeypatch):
    lay, notes, work, bare = two_repos
    prom = tmp_path / "merlin_index.prom"
    monkeypatch.setenv("METRICS_FILE", str(prom))
    ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    first = dict(l.rsplit(" ", 1) for l in prom.read_text().splitlines() if not l.startswith("#"))
    assert first['merlin_index_repo_documents{repo="infra"}'] == "3"
    assert first['merlin_index_repo_documents{repo="notes"}'] == "7"
    assert int(first['merlin_index_repo_chunks{repo="notes"}']) > 7
    assert first['merlin_index_repo_behind_mirror{repo="infra"}'] == "0" == first['merlin_index_repo_behind_mirror{repo="notes"}']
    assert not (tmp_path / "merlin_index.prom.tmp").exists()
    conn.execute("UPDATE meta SET value = '{\"infra\": 1, \"notes\": 1}' WHERE key = 'repo_indexed_at'")   # age them
    (notes / "planning/backlog.md").write_text("# Backlog\n- moved\n")
    sh(notes, "commit", "-qam", "edit")
    ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    second = dict(l.rsplit(" ", 1) for l in prom.read_text().splitlines() if not l.startswith("#"))
    assert second['merlin_index_repo_indexed_timestamp_seconds{repo="infra"}'] == "1"      # unchanged repo: kept
    assert int(second['merlin_index_repo_indexed_timestamp_seconds{repo="notes"}']) > 1    # moved: now
    (notes / "planning/backlog.md").write_text("# Backlog\n- after the run\n")
    sh(notes, "commit", "-qam", "late")
    from merlin import metrics
    heads = json.loads(config.get_meta(conn)["heads"])
    metrics.write(conn, str(prom), list(lay.repos), heads, {}, 0)
    assert 'merlin_index_repo_behind_mirror{repo="notes"} 1' in prom.read_text()            # HEAD re-read


@pytest.mark.parametrize("polluted", [False, True])
@pytest.mark.skipif(not (Path(__file__).resolve().parent.parent / "legacy" / "sql").is_dir(),
                    reason="needs merlin v1's legacy/sql (only in deployments that kept v1)")
def test_refuses_merlin_v1_schema(repo, polluted):
    """merlin v1's real schema (legacy/sql), as a role's default search_path would expose it. Unguarded, ingest
    created `meta` there and failed part-way; it must refuse before writing, also after such a run (polluted)."""
    from pathlib import Path
    legacy = Path(__file__).resolve().parent.parent / "legacy" / "sql"
    schema = f"merlin_v1_{uuid.uuid4().hex[:8]}"
    admin = config.connect()
    admin.execute(f"CREATE SCHEMA {schema}")
    try:
        c = config.connect(options=f"-c search_path={schema},public")
        for f in ("001_schema.sql", "002_chunk_hash.sql"):
            c.execute((legacy / f).read_text().replace("docs.", f"{schema}.").replace("BEGIN;", "").replace("COMMIT;", ""))
        c.execute("INSERT INTO documents (repo, path, type, commit, blob_sha, bytes) "
                  "VALUES ('fleet', 'hosts/alpha.md', 'fleet-host', 'c', 'x', 1)")
        if polluted:
            c.execute("CREATE TABLE meta (key text PRIMARY KEY, value text NOT NULL)")
        with pytest.raises(RuntimeError, match="not a merlin index"):
            ingest.run(c, str(repo), CFG, embedder=FakeEmbedder())
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert (c.execute("SELECT to_regclass('meta')").fetchone()[0] is not None) == polluted   # nothing created
        c.close()
    finally:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


def test_db_schema_pins_search_path(monkeypatch):
    schema = f"merlin_pin_{uuid.uuid4().hex[:8]}"
    admin = config.connect()
    admin.execute(f"CREATE SCHEMA {schema}")
    try:
        monkeypatch.setattr(config, "DB_SCHEMA", schema)
        with config.connect() as c:
            assert c.execute("SELECT current_schema()").fetchone()[0] == schema
        monkeypatch.setattr(config, "DB_SCHEMA", "bad; drop")
        with pytest.raises(ValueError, match="plain lower-case identifier"):
            config.connect()
    finally:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


def test_chunk_cap_change_rechunks_and_vector_fusion(conn, repo):
    from dataclasses import replace
    lay = Layout.from_toml(DEFAULT_TOML)
    s = ingest.run(conn, str(repo), CFG, embedder=FakeEmbedder(), layout=lay)
    small = replace(lay, chunk_max_tokens=120)
    s = ingest.run(conn, str(repo), CFG, embedder=FakeEmbedder(), layout=small)
    assert s["changed"] == s["docs"] == 10                                   # every document re-chunked
    assert json.loads(config.get_meta(conn)["chunk_config"]) == {"max_tokens": 120}
    assert ingest.run(conn, str(repo), CFG, embedder=FakeEmbedder(), layout=small)["changed"] == 0
    vec = replace(small, search_fusion="vector")
    assert search.search(conn, "scrub checksum errors cable", layout=vec)
    ex = search.search(conn, "Failed to restore initial working directory", exact=True, layout=vec)
    assert ex[0].path == "journal/2030/01/2030-01-15.md"                      # exact still uses ILIKE + full-text


def test_mcp_v1_compatible_tools(conn, two_repos, monkeypatch):
    """The tool parameters sessions already use with v1: search(per_document=False), list_documents(types/repos/
    since/until/path_contains/limit), get_document(repo=, path=)."""
    from merlin import mcp_server
    lay, notes, work, bare = two_repos
    ingest.run(conn, None, CFG, embedder=FakeEmbedder(), layout=lay)
    monkeypatch.setattr(mcp_server, "LAYOUT", lay)
    monkeypatch.setattr(mcp_server, "_conn", conn)
    monkeypatch.setattr(search, "load_layout", lambda *a, **k: lay)
    grouped = mcp_server.search("scrub checksum errors cable disk", k=10)
    raw = mcp_server.search("scrub checksum errors cable disk", k=10, per_document=False)
    paths = lambda out: [l.split()[2] for l in out.splitlines() if l[:1].isdigit() and ". [" in l]
    assert len(set(paths(grouped))) == len(paths(grouped))                  # one per document
    assert len(set(paths(raw))) < len(paths(raw))                           # raw: a document can repeat
    lines = lambda out: [l for l in out.split("\n\n", 1)[1].splitlines()]
    assert all(" · runbook · " in l for l in lines(mcp_server.list_documents(types=["runbook"])))
    assert all(l.startswith("infra/") for l in lines(mcp_server.list_documents(repos=["infra"])))
    assert lines(mcp_server.list_documents(type="host")) == lines(mcp_server.list_documents(types=["host"]))
    assert [l.split(" · ")[0] for l in lines(mcp_server.list_documents(path_contains="ALPHA"))] == ["infra/hosts/alpha.md"]
    assert lines(mcp_server.list_documents(path_contains="_x%")) == ["No documents."]      # wildcards are literal
    assert len(lines(mcp_server.list_documents(limit=2))) == 2
    dated = [l.split(" · ")[2] for l in lines(mcp_server.list_documents(types=["journal"]))]
    assert dated == sorted(dated, reverse=True)                             # newest first
    assert len(lines(mcp_server.list_documents(types=["journal"], since="2030-01-15", until="2030-01-15"))) == 1
    assert "[host] infra/hosts/alpha.md" in mcp_server.get_document(repo="infra", path="hosts/alpha.md")



@pytest.fixture
def public_index(repo):
    """A merlin index in `public`, as on a live deployment: the situation that exposed the DB_SCHEMA fall-through."""
    pub = config.connect(options="-c search_path=public")
    if config.own_tables(pub):
        pub.close()
        pytest.skip("public already holds tables in this database")
    ingest.run(pub, str(repo), CFG, embedder=FakeEmbedder())
    yield pub
    for t in ("chunks", "documents", "meta"):
        pub.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")
    pub.close()


def test_db_schema_empty_target_does_not_touch_public(monkeypatch, repo, public_index):
    """2026-10-04 incident: DB_SCHEMA pointed at an empty schema, unqualified names resolved in public, and the
    migrations ran on the live tables. Now: the index is built in the pinned schema and public is untouched."""
    cols = lambda: [r[0] for r in public_index.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='documents' "
        "ORDER BY ordinal_position").fetchall()]
    before_cols, before_meta = cols(), dict(public_index.execute("SELECT key, value FROM public.meta").fetchall())
    schema = f"merlin_fresh_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(config, "DB_SCHEMA", schema)
    with pytest.raises(RuntimeError, match="does not exist"):
        config.connect()                                              # a missing schema is an error for readers
    c = config.connect(create_schema=True)
    try:
        assert config.get_meta(c) == {}                               # public's meta is not this schema's meta
        with pytest.raises(RuntimeError, match="no merlin index"):
            config.require_index(c)
        s = ingest.run(c, str(repo), CFG, embedder=FakeEmbedder())
        assert (s["docs"], s["changed"]) == (10, 10)                  # a fresh build, not public's "unchanged"
        assert config.own_tables(c) == set(config.INDEX_TABLES)
        assert c.execute(f"SELECT count(*) FROM {schema}.documents").fetchone()[0] == 10
        config.require_index(c)
        assert cols() == before_cols                                   # no migration touched public
        assert dict(public_index.execute("SELECT key, value FROM public.meta").fetchall()) == before_meta
    finally:
        c.close()
        public_index.execute(f"DROP SCHEMA {schema} CASCADE")


def test_db_schema_fresh_database_keeps_pgvector_in_public(monkeypatch, repo):
    """2026-10-05: on a database without pgvector, the first pinned-schema ingest installed the extension into that
    schema, and the next schema's ingest failed `type "vector" does not exist`. It must land in public."""
    admin = config.connect(options="-c search_path=public")
    name = f"merlin_fresh_db_{uuid.uuid4().hex[:8]}"
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error:
        admin.close()
        pytest.skip("role cannot create databases")
    url = psycopg.conninfo.make_conninfo(config.DATABASE_URL, dbname=name)
    try:
        for schema in ("pin_a", "pin_b"):
            monkeypatch.setattr(config, "DB_SCHEMA", schema)
            with config.connect(url, create_schema=True) as c:
                s = ingest.run(c, str(repo), CFG, embedder=FakeEmbedder())
                assert (s["docs"], s["changed"]) == (10, 10)
                assert config.own_tables(c) == set(config.INDEX_TABLES)
                assert c.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
                                 "WHERE e.extname = 'vector'").fetchone()[0] == "public"
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_indexed_schemas_sees_an_index_outside_the_test_schemas():
    """The guard behind `refuse_indexed_database`: a deployment's index counts, the suite's own schemas don't."""
    live, mine = f"guardcheck_{uuid.uuid4().hex[:8]}", f"merlin_test_{uuid.uuid4().hex[:8]}"
    admin = config.connect(options="-c search_path=public")
    try:
        for schema in (live, mine):
            admin.execute(f"CREATE SCHEMA {schema}")
            admin.execute(f"CREATE TABLE {schema}.meta (key text PRIMARY KEY, value text NOT NULL)")
        found = indexed_schemas(admin)
        assert live in found and mine not in found
    finally:
        for schema in (live, mine):
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()
