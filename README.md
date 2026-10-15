# merlin

The memory and search layer for a **files-canonical Claude Code working partner**. The partner keeps its record as
plain markdown in git: a daily journal of what was actually seen, host records, runbooks, short lessons and plans.
merlin indexes that record for **hybrid search** (semantic plus full-text) and **host history**, and serves
both to Claude Code as a read-only MCP server.

merlin grew out of the memory engine of Anna, a personal assistant project.

**Principle: the files are canonical, and the index is derived.** Every row can be rebuilt from git at any time.
Nothing is ever written only to the database: no notes, no edits, no state you would miss if you dropped it.

## How it works

```
 git repo (markdown)        ingest (timer)                              Postgres 17 + pgvector
 journal/ hosts/      ──►  pull --ff-only → walk HEAD → skip unchanged  ──►  documents (path, type, date, blob sha)
 runbooks/ memory/          blobs → redact secrets → chunk by ##/###          chunks (heading, body, hosts[], date,
 planning/ skills/          → embed (reusing vectors by text sha)                     vector, tsvector)
                                                                                   │
 Claude Code ◄── stdio (over ssh) ── mcp_server: search · host_history ◄───────────┘
                                     get_document · list_documents · index_status
```

- **Chunking** (`chunk.py`). One chunk per `##`/`###` section, never split inside a code fence. A section over
  about 1,500 tokens is split at paragraph boundaries. Each chunk's embedded text starts with a context header,
  `[type] path > H1 > H2 > H3`, so a section called "Verify" still says which runbook it belongs to.
  - **Hosts**: the `host:` front matter field, plus any mention of a known host, alias or IP. The known hosts
    come from the front matter of the host records.
  - **Dates**: the filename date for the journal, else a front matter `date`/`updated`/`created`, else the
    file's last commit date. A section takes the date from its own heading when it has one, such as
    `## 2030-01-16 (session 2)` or `### Disk full (2030-01-15)`.
- **Ingest** (`ingest.py`) runs one transaction per document.
  - Documents whose blob sha is unchanged are skipped. Changed ones are re-chunked, and embeddings are reused by
    the sha256 of the exact embedded text, so editing one paragraph re-embeds only that chunk. Removed documents
    are deleted.
  - Secrets are redacted before anything is chunked or stored, and again when a document is served: tokens
    (including Telegram bot tokens), `sk-` keys, hex strings of 32+ characters, JWTs, private-key blocks,
    `key=value` secrets and credentials in URLs. See [Redaction](#redaction).
  - Problems that would silently hurt retrieval are printed to stderr as `warning: <path>: …` (see
    [Ingest warnings](#ingest-warnings)). They never stop the run.
  - The run records `head_sha`, `last_ingest_at` and the counts in `meta`.
- **Search** (`search.py`) fuses a vector top-N (HNSW cosine) with Postgres full-text search
  (`websearch_to_tsquery` + `ts_rank_cd`, headings weighted above bodies) by reciprocal-rank fusion (k = 60).
  - Filters: type, host (an alias works too), and since/until, matched against the section's date.
  - The fused scores are then reranked by doc type, recency and stale markers, and grouped to one section per
    document, naming the document's other matching sections. See [Ranking](#ranking).
  - Filtered vector scans use `hnsw.iterative_scan = relaxed_order` with a raised `ef_search`, so a filter
    can't starve the result.
  - `exact=true` drops the embedding and fuses a literal `ILIKE` with full-text search. Use it for error
    strings, paths and IDs.
- **Host history** returns one host's journal sections and host-record notes **newest first**, by date rather
  than by relevance. Use it to read "what have we already done or hit on this host" before running a procedure
  there. With `topic=`, it keeps the best hybrid matches within that host and still returns them in date order.
  Undated host-record sections carry the file's commit date, so the current record sorts above the evidence.

## Doc types and how to weigh them

The shipped layout maps paths to types: `journal/**` is journal, `hosts/*.md` host, `runbooks/**` runbook,
`memory/*.md` memory (`memory/MEMORY.md` is memory-index), `planning/**` planning, `skills/**` skill, and top-level
`*.md` doc. `_template.md` and non-markdown files are skipped. The MCP tool descriptions tell the model how to
weigh each type:

| Type | Weigh as |
|---|---|
| journal | what was seen on that date: evidence, possibly superseded |
| host, runbook | current practice |
| memory | lessons and gotchas, with pointers to the detail |
| planning | intent, not fact |

The tool descriptions also say that **an empty result is not proof of absence**.

To use a different layout, copy `merlin.example.toml` to `merlin.toml`, or point `MERLIN_CONFIG` at your copy.
It holds the glob → type rules (first match wins), the per-type weighting text, which types define hosts, which
types take their date from the filename, and which types feed `host_history`.

### Several repos as one corpus

A corpus can span several git repos (for example one repo per layer). List them in the layout file:

```toml
[[repos]]
name = "notes"
dir = "/srv/merlin/git/notes.git"     # a bare mirror or a checkout
[[repos]]
name = "infra"
dir = "/srv/merlin/git/infra.git"
```

Each file is then indexed under the virtual path `<name>/<path>` (`infra/hosts/db1.md`). That is what the layer
globs match (`{ glob = "infra/hosts/*.md", type = "host" }`), what search returns, and what `get_document` takes
(or `repo=` plus the path inside it). `search` gains `repos=[…]`, `list_documents` gains `repo=`, and
`index_status` reports each repo's indexed and current HEAD. Host records in any repo tag mentions in all of them.
Every repo is listed before anything is written, so a repo that cannot be read stops the run instead of having its
documents deleted. Without `[[repos]]`, `REPO_DIR` is the corpus and paths carry no prefix. Adding `[[repos]]` to an
existing index changes every path, so every document is re-indexed once.

## Index freshness

Every `search`, `host_history` and `list_documents` result starts with one verdict line, and `index_status` adds the
per-repo detail (indexed vs current HEAD, last ingest, model, counts):

| Verdict | When |
|---|---|
| `index fresh (at HEAD)` / `index fresh (all repos at their HEAD)` | every repo's HEAD is indexed |
| `index fresh; catching up on commits from the last few minutes: <repo> (N min)` | a commit is unindexed but younger than 30 min: normal between timer runs |
| `⚠ INDEX STALE: <reasons> — results may be missing recent changes.` | an unindexed commit older than 30 min ("ingest broken?"), a repo never indexed, a HEAD that cannot be read, a last ingest older than 30 min, or a failed last ingest |

The grace period matters: warning on every normal gap between timer runs trains readers to ignore the warning. The
HEAD compared is the last-fetched upstream when the checkout tracks one, else HEAD. A verdict that cannot read a
repo says STALE, never fresh.

## Redaction

Two layers, set in the layout's `[redact]` table:

- **Secret hashes** (`secret_hashes = "<path>"`, or `SECRET_HASHES` in the environment, which wins). A file of salted
  sha256 hashes of the live secrets, generated where the secrets are kept, so only hashes reach the index host:
  line 1 `salt <hex>`, then `<sha256(salt + value)>\t<name>` per secret. Every whole token whose salted hash
  matches becomes `[REDACTED:<name>]` (tokens split on whitespace, quotes and `=:,()<>[]{}|;`; a trailing `=` run is
  kept so padded base64 matches; trailing sentence punctuation is also tried). This catches a live secret in any
  shape, including weak ones no pattern would. **Once configured it is required:** a missing, malformed or empty file
  stops ingest and the MCP server (fail closed), and a failed ingest is reported by `index_status`.
- **Pattern families** for well-known shapes: `private_key`, `url_credentials`, `sk_key`, `vendor_token`, `jwt`,
  `bearer`, `key_value`, `hex32`. `disable = [...]` turns families off where a shape is mostly evidence: a record
  full of checksums, digests and commit IDs is a reason to disable `hex32` (and rely on the hashes for live
  secrets). Unknown names are refused.

Hashes run first, so a named redaction is not re-redacted by a pattern.

## Ranking

`search` scores in four steps. Every number below is the shipped default, and all but the last step are set in
the layout file (`merlin.toml`).

1. **Fusion.** The vector leg (or the literal `ILIKE` leg with `exact=true`) and the full-text leg each return up to
   `max(50, 8k)` chunks; RRF scores each chunk `Σ 1/(60 + rank)`.
2. **Rerank.** Each fused score is multiplied by three factors:
   - **Doc-type boost** (`[boost]`): host, runbook, memory 1.3; journal 1.0; planning 0.9; doc 0.8; skill 0.7;
     memory-index 0.6. Unlisted types get 1.0.
   - **Recency** (`[recency]`): `1 + weight · 0.5^(age_days / half_life_days)`, with weight 0.05 and a 30-day half
     life, so a section dated today gets at most +5%. The age uses the section's date, else the document's. It is a
     tiebreak, not a sort.
   - **Stale** (`[stale]`): × 0.5 when the section marks itself superseded or obsolete, either in its own heading
     (`heading` regex, matched anywhere) or at the start of its first non-blank line (`lead` regex). See
     [Corpus conventions](#corpus-conventions-that-help-retrieval).
     With `inherit = ["journal"]` (off by default), a chunk of such a doc whose heading chain is an ancestor of a
     matched stale section is penalized too: a journal's day index restates what the superseded section said.
3. **Per-document grouping** (`per_document`, on by default). One result per document, ordered by the document's
   best score. The section shown is normally the best one, but among the sections scoring within 10% of it
   (`NEAR_TIE`), an unmarked section beats a stale one, and then a newer-dated section beats an older one, because
   a later section usually supersedes an earlier one. The document keeps its best score. `more` counts the
   document's other candidate chunks, and `also` names the headings of up to two other sections (`ALSO`), which the
   MCP output shows as an `also: § … · § …` line under the heading, so the reader knows which section to open with
   `get_document`.
4. **Cut** to `k` results.

Why it is needed: RRF scores are nearly flat (roughly 0.016 to 0.03 for the whole result list), so without the
rerank, a journal entry or index file that merely *mentions* a topic ties with, and often beats, the host record,
runbook or lesson that is the canonical answer. On the reference corpus, the boost and recency step raised hit@1
from 57% to 70%, and section-level grouping raised it to 74% (see [design notes](docs/design-notes.md)).

**Tuning.** Copy `merlin.example.toml` to `merlin.toml` and edit `[boost]`, `[recency]` and `[stale]`; set a
factor to 1.0 or a weight to 0 to switch a step off. Measure every change with the [evaluation](#evaluation)
before and after: try overrides with `--boost` and `--recency` first, then write the winner to `merlin.toml`.
Prefer moderate values. On a small case set, a stronger boost that scores slightly higher is usually
overfitting. `NEAR_TIE` and `ALSO` are constants in `search.py`, not layout keys. `host_history` does not rerank:
it is ordered by date.

## Corpus conventions that help retrieval

The index can only rank what the files say. These conventions make the right section win:

- **Mark superseded sections at the top.** Start the section's first line with `(superseded YYYY-MM-DD: see § X)`,
  or put *superseded* or *obsolete* in its heading. The stale penalty only fires on those two positions: a passing
  mention of "superseded" further down the body doesn't count, deliberately. (Conversely, a heading such as "Why the
  old plan was superseded" does count.) Pointing at the replacement (`see § X`) lets the reader follow it.
- **Keep front matter valid YAML.** Quote any value that contains `: ` (for example
  `description: "fix: restart the agent"`). Invalid front matter is ignored as a whole, so a host record silently
  loses its `aliases:` and `ips:` and its host tagging. Ingest now warns about it.
- **Balance code fences.** One unclosed fence inverts every later fence in the file. Ingest recovers (see below)
  and warns, but fix the file. Note that `` ``` `` and `~~~` both toggle the fence state.
- **Put the summary at the top of a document.** The first section of a host record or runbook is what a "what
  is X / how is X set up" question should land on; a short facts block there (role, address, OS, what runs on it)
  beats facts scattered through later sections. Generating that block from a source of truth works well.
- **Give sections specific headings and, where relevant, a date** (`### Disk full (2030-01-15)`): the heading is
  weighted above the body in full-text search, and the date feeds the recency factor and the near-tie pick.

## Ingest warnings

Ingest prints these to stderr and indexes the document anyway:

| Warning | Cause | Effect if ignored |
|---|---|---|
| `front matter is not valid YAML (quote values containing ': '); its fields are ignored` | the `---` block doesn't parse | no `host:`, `aliases:`, `ips:`, `date:` from that file |
| `code fences don't balance (last unmatched opener at body line N); recovered at ##/### headings` | an odd number of fence lines | recovered: inside an unbalanced document, a `##`/`###` line ends a "fence"; balanced documents are untouched, so they may still quote markdown headings inside code blocks |

Before the recovery, a single unclosed fence in a long journal file merged hundreds of lines into one chunk, with
the wrong heading and the union of every host mentioned; that chunk matched almost every query.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://merlin@127.0.0.1:5432/merlin` | libpq URL; put the password in `PGPASSWORD` |
| `REPO_DIR` | `/repo` | the git checkout to index (single-repo corpus) |
| `DB_SCHEMA` | — (the role's `search_path`) | pin the index to this schema: each connection's `search_path` becomes `<schema>, public`, overriding a per-role default |
| `MERLIN_CONFIG` | `./merlin.toml` if present | layout file (see above) |
| `TZ` | UTC | IANA zone used to display times; dates themselves come from filenames and front matter |
| `EMBED_PROVIDER` | `tei` | `tei` · `openai` (or any OpenAI-compatible server) · `voyage` · `ollama` |
| `EMBED_URL` | provider default (`http://127.0.0.1:8080` for TEI) | base URL for **document** embedding |
| `EMBED_URL_QUERY` | `EMBED_URL` | base URL for **query** embedding only |
| `EMBED_MODEL` | `nomic-ai/nomic-embed-text-v1.5` (TEI) | model name |
| `EMBED_DIM` | `768` | vector size; fixes `vector(N)` when the schema is created |
| `EMBED_API_KEY_FILE` | — | path to a file holding the API key; the key is never read from an env var or logged |
| `EMBED_DOC_PREFIX` / `EMBED_QUERY_PREFIX` | nomic's `search_document: ` / `search_query: ` for nomic models on TEI/Ollama, else empty | text prefixes |
| `EMBED_BATCH` | `16` | texts per embedding request |
| `SECRET_HASHES` | layout `[redact] secret_hashes` | the secret-hash file (see [Redaction](#redaction)); overrides the layout |
| `METRICS_FILE` | — | node_exporter textfile to write after each successful ingest (`merlin_index_*`, see `merlin/metrics.py`) |

Voyage gets `input_type=document|query`. The OpenAI adapter sends `dimensions` for `text-embedding-3-*` models.

**Model-identity guard.** The first ingest stores `embed_model_id` (`provider:model:dim`) in `meta`. Every later
ingest and every query compares it with the configuration and refuses to run on a mismatch:
`index was built with X; configured Y; run ingest --reembed-all`. Vectors from different models are never mixed.
With TEI, the server's `/info` model id is also checked against `EMBED_MODEL`.

**Split document and query backends.** For bulk embedding on a GPU box with queries served by a local CPU TEI,
set `EMBED_URL` to the GPU instance and `EMBED_URL_QUERY` to the local one. **Both must serve the same model with
the same weights.** TEI on CPU and on GPU with identical weights agree to a cosine of about 1.0. A different model
would silently ruin retrieval; the `/info` check catches that for TEI. If the document backend is unreachable,
ingest fails cleanly: documents are atomic, nothing falls back to another model, and the error is recorded in
`meta.last_ingest_error`, which `index_status` reports.

## Running

```bash
bin/ingest --pull          # incremental; safe to run from a timer every few minutes
bin/ingest --reembed-all   # after changing the embedding model: drops every vector and rebuilds
bin/mcp                    # MCP server on stdio
```

With Docker, add an `app` service to the compose project that runs Postgres and TEI. The example below assumes
those services are called `db` and `embed`:

```yaml
  app:
    build: ./merlin                 # this repo
    environment:
      DATABASE_URL: postgresql://merlin@db:5432/merlin
      PGPASSWORD: ${POSTGRES_PASSWORD}
      EMBED_URL: http://embed:80
      REPO_DIR: /repo
      TZ: ${TZ:-UTC}
    volumes:
      - /srv/merlin/git/corpus:/repo    # writable by uid 10001 if ingest uses --pull
    depends_on: [db, embed]
    profiles: [tools]                   # run on demand only: `docker compose run --rm app ...`
```

```bash
docker compose -f /opt/merlin/compose.yaml run --rm app bin/ingest --pull
```

### Connecting Claude Code

The MCP server runs over stdio through SSH from the workstation:

```bash
claude mcp add merlin -- ssh user@index-host \
  docker compose -f /opt/merlin/compose.yaml run --rm -T app python -m merlin.mcp_server
```

`-T` matters: without it, compose allocates a TTY, which corrupts the stdio JSON-RPC stream. Each Claude Code
session starts a short-lived container. Call `index_status` first in a session, and before trusting that
something is absent.

## Evaluation

`merlin/evaluate.py` runs a fixed question set through `search()` and reports **hit@1**, **hit@3** and
**MRR** (mean reciprocal rank of the first acceptable result, 0 when it isn't in the top `k`). Run it after every
ranking change and after large corpus edits. A ranking change made without measurement can easily make some
query worse.

The cases belong to the **corpus repo**, not this one, because what counts as the right answer is a fact about the
corpus:

```yaml
cases:
  - q: how do I restore the database from last night's dump?      # phrased the way you'd actually ask
    expect:
      - path: runbooks/postgres/restore.md                          # any listed answer counts
      - path: hosts/db1.md
        heading: backups                                            # optional: substring of the shown section heading, case-insensitive
  - q: why does the backup job fail after a kernel update?
    expect: [{path: memory/backup-kernel-module.md}]
```

```bash
python -m merlin.evaluate <cases.yaml> [-k 10] [--json] [--boost JSON] [--recency F]
# with Docker (the corpus checkout is mounted at /repo):
docker compose run --rm -T app python -m merlin.evaluate /repo/<path/to/eval.yaml>
```

- `-k`: results fetched per question (default 10).
- `--json`: summary plus per-case rank and top 3, for diffing runs.
- `--boost '{"host": 1.5, "runbook": 1.5}'`: **replaces** the whole `[boost]` table for this run (unlisted types
  become 1.0); `--boost '{}'` turns boosting off.
- `--recency 0`: overrides the recency weight (0 = off). There is no override for `[stale]`; edit a copy of the
  layout and point `MERLIN_CONFIG` at it.

Each case prints ✅ (rank 1), ⚠ (rank 2–3) or ⛔ (lower or missing), and every miss lists the top three
`path § heading` results, which usually shows whether the fix belongs in the ranking or in the corpus. The last
line is the summary: `cases 26 · hit@1 81% · hit@3 100% · MRR 0.885`.

**Self-quote guard.** Real questions end up pasted into journals and retrieval logs, and a document that quotes a question
verbatim can rank for it. The eval flags every top-3 hit whose document contains the case's question verbatim (`⚑ #2 <path> quotes the
question verbatim — not an answer; clean rank N`) and prints a second line with hit@1 / hit@3 / MRR **without** such documents
(answers that happen to quote it are kept). A gap between the two lines means the score is inflated by the record quoting itself.
Keep retrieval logs out of the corpus, and prefer vector fusion: full-text rewards an exact quote.

**Growing the set.** Add a case whenever a search in real use returns the wrong thing, phrased as it was asked,
with every document that would be an acceptable answer. Use `heading` only when the document is long and only
one section answers. Keep cases you already pass: they guard against regressions. With a few dozen cases, one
case is several percentage points, so prefer fixes that are principled (a convention, a general ranking rule) over
weights tuned to the last miss.

## Tests

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

The unit tests run against the synthetic corpus in `tests/fixtures/corpus/`, which has fictional hosts `alpha` and
`beta`. The other tests depend on the environment:

- `tests/test_integration.py` needs `DATABASE_URL`. It creates and drops a throwaway schema, and uses a
  deterministic fake embedder.
  It refuses (exit code 3) a database that already holds merlin index tables, so point it at a throwaway
  database, never a deployment's.
- `test_real_embedder` also needs `EMBED_URL`.
- `CORPUS_DIR=/path/to/repo` additionally checks the chunker against a real corpus.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
