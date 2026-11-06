# merlin

The memory and search layer for a **files-canonical Claude Code working partner**. The partner keeps its record as
plain markdown in git: a daily journal of what was actually seen, one record per thing the work is about, how-tos,
short lessons and plans. merlin indexes that record for **hybrid search** (semantic plus full-text) and
**per-entity history**, and serves both to Claude Code as a read-only MCP server.

It exists because an assistant's memory is the weak point. A session only knows what is in its context; a long one
gets compacted into a summary, and whatever the summary dropped gets filled in with something plausible. merlin's
answer is to keep the memory outside the model, in files a person can read and correct, and to make the assistant
look things up there (with the source path and date) instead of recalling them:

- **Evidence and current practice are different types.** A journal entry says what was seen on a date; a record or
  how-to says how things are now. The tools tell the model which is which, rank the canonical one higher, and
  demote sections marked superseded.
- **History per entity.** "What have we already done or hit on X" is a chronological question, so it gets its own
  tool (`history`), not a relevance search.
- **An empty result is not proof of absence**, and every answer carries an index-freshness verdict.
- **The record is only as good as what sessions write back.** merlin reads; the assistant writes. A short
  [session loop](docs/reference.md#the-session-loop-keeping-the-record) (look it up first, write it down at the end) is what keeps
  the record current.

The shipped layout is for infrastructure (the entity is a *host*: journal, host records, runbooks), which is where
it came from. The layout is configuration: [examples/](examples/) has an [Anna](examples/anna/) record, a software project
(components, decisions) and a learning record (topics, lessons, open questions).

merlin grew out of the memory engine of [Anna](https://github.com/kaibae19/anna), a scaffold for a personal agent
built on Claude Code.

**Principle: the files are canonical, and the index is derived.** Every row can be rebuilt from git at any time.
Nothing is ever written only to the database: no notes, no edits, no state you would miss if you dropped it.

## Quick start

Everything runs on one machine with Docker: Postgres with pgvector, a CPU embedder and merlin. You need Docker with
Compose, about 2 GB of free memory and a few GB of disk, and your record as a git checkout on that machine.

```bash
git clone https://github.com/kaibae19/merlin ~/merlin && cd ~/merlin/deploy
cp .env.example .env          # set POSTGRES_PASSWORD and RECORD_DIR; MERLIN_TOML for a layout other than the default
docker compose up -d          # starts the database and the embedder; the first start downloads the model (~550 MB)
docker compose logs -f embed  # wait for "Ready", then Ctrl-C
docker compose run --rm app bin/ingest      # builds merlin and indexes the record
```

The first index embeds every section of the record: a new record of about 30 files (100 sections) took 7 minutes
on a 2-core VM, and a large record on a slow CPU can take hours (a GPU embedder or a hosted one helps: [Configuration](docs/reference.md#configuration-environment)). Later runs
only embed what changed.

merlin indexes **committed** files (it reads the checkout's HEAD), so commit what the assistant writes.

### Connecting Claude Code

```bash
claude mcp add --scope user merlin -- \
  docker compose -f ~/merlin/deploy/compose.yaml run --rm -T app python -m merlin.mcp_server
```

`-T` matters: without it, compose allocates a TTY, which corrupts the MCP stream. If merlin runs on another machine,
put `ssh user@that-host` in front of `docker compose`. In a new session, ask Claude Code to call `index_status`: it
should say `index fresh`.

What to put in `CLAUDE.md` so the assistant actually uses the record: [examples/claude-code/](examples/claude-code/README.md).

### Keeping it fresh

Run the ingest every few minutes; unchanged documents are skipped, so a run with nothing new takes seconds. With cron:

```
*/10 * * * *  docker compose -f $HOME/merlin/deploy/compose.yaml run --rm app bin/ingest >/dev/null
```

Every search result starts with an [index-freshness verdict](docs/reference.md#index-freshness), so a stopped timer
shows up as `INDEX STALE` instead of as silently missing results.

## Layouts

The shipped layout is for infrastructure. Pick the example closest to your record and set `MERLIN_TOML` to it:

| Example | Entity | For |
|---|---|---|
| [infrastructure](examples/infrastructure/) (the default) | host | a homelab or ops record: journal, host records, runbooks, lessons |
| [anna](examples/anna/) | host | the record of an [Anna](examples/anna/README.md) assistant |
| [software-project](examples/software-project/) | component | a codebase: components, decisions, how-tos |
| [learning](examples/learning/) | topic | studying something: topics, lessons, open questions, sources |

How the types are weighed and how to write your own: [Doc types](docs/reference.md#doc-types-and-how-to-weigh-them).

## Documentation

- [Reference](docs/reference.md): how it works, doc types and entities, index freshness, redaction, ranking, corpus
  conventions, [the session loop](docs/reference.md#the-session-loop-keeping-the-record), configuration, running it
  in your own compose project, evaluation, tests
- [Design notes](docs/design-notes.md): why it is built this way
- [examples/claude-code/](examples/claude-code/README.md): wiring it into Claude Code's instructions
- [CHANGELOG](CHANGELOG.md)

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
