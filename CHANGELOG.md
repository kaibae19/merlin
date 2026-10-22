# Changelog

## Unreleased

- **Entities** generalize hosts: `[entities]` sets what the record's things are called (`name`), which doc types
  define them, the front matter key and alias fields. The default stays `host` with `aliases` + `ips`; the old
  top-level `host_types` and `[hosts] text_ignore` still load.
- **`history(entity=…)`** and the `entity=` search filter. With `name = "host"`, `host_history(host=…)` and
  `search(host=…)` stay as aliases. Eval cases take `entity:` (`host:` still works).
- The "prefer the more canonical source" rule in the MCP instructions names the layout's boosted types instead of
  a hardcoded host/runbook.
- **`canonical`** (top-level layout key) names the types that rule calls canonical, when that differs from the
  boosted ones: a boosted type isn't always canonical (lessons can rank high and still go stale). It only changes
  the instruction text, not ranking. An unknown type is an error. The shipped default sets `["host", "runbook"]`
  (memory is boosted, not canonical), and each example sets its own.
- **The session loop** (README): how the record stays current (look up first, journal with real output, update
  the canonical layers, handoff, retrieval retrospective, commit), plus a generic wrap-up skill in
  `examples/skills/wrapup/`.
- **Using it from Claude Code** (`examples/claude-code/`): a `CLAUDE.md` block, and how the assistant uses the
  tools day to day, in its own words.
- **Example layouts** in `examples/`: infrastructure (the default), a software project, a learning record. The
  README now explains the idea before the infrastructure layout.

## 0.1.0 (2026-10-15): first public release

merlin indexes a files-canonical markdown record (journal, host records, runbooks, lessons, plans) for hybrid search
and serves it to Claude Code as a read-only MCP server. This release contains:

- **Ingest:** one chunk per `##`/`###` section with a context header; host tagging from host-record front matter;
  section dates from headings, filenames or front matter; incremental by blob sha, with embeddings reused by text
  hash; warnings for invalid front matter and unbalanced code fences (with recovery).
- **Several git repos as one corpus** (`[[repos]]`), each indexed under `<name>/<path>`.
- **Search:** vector + full-text reciprocal-rank fusion (or vector only, `[search] fusion`), reranked by doc-type
  boost, recency and stale markers, grouped to one section per document with a near-tie pick and `also` headings;
  `exact=true` for literal strings.
- **Host history:** one host's sections newest first, optionally led by its own record (`[history] record_chunks`)
  and with path excludes.
- **Redaction** before storage and when serving: salted secret hashes (fail closed once configured) plus switchable
  pattern families.
- **Freshness verdict** on every result, with a 30-minute grace period for unindexed commits; `index_status`.
- **Embedding providers:** TEI, OpenAI-compatible, Voyage, Ollama; split document/query backends; a model-identity
  guard against mixing vectors.
- **Postgres schema pinning** (`DB_SCHEMA`) with guards against writing into another application's tables.
- **Retrieval eval** (`merlin.evaluate`): hit@1, hit@3, MRR, with a self-quote guard.
- **Prometheus textfile metrics** (`METRICS_FILE`).
- **Integration tests** that refuse a database already holding a merlin index.

Why the ranking and ingest work the way they do: [`docs/design-notes.md`](docs/design-notes.md).
