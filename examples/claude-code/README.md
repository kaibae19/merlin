# Using merlin from Claude Code

Connecting the MCP server is in the main [README](../../README.md#connecting-claude-code). This is the other half:
what to put in the assistant's instructions so it actually uses the record, and, written by the assistant that runs
this setup every day, what that looks like from its side.

## Wiring it up

1. **Connect** the server (`claude mcp add merlin -- ssh … python -m merlin.mcp_server`) and allow its tools
   (`mcp__merlin__*` in the permissions allowlist), so lookups don't each ask for approval. They are all read-only.
2. **Start sessions in the same place.** Launch Claude Code from one home directory, with a `CLAUDE.md` that points
   at the record and says how to use it. A session that starts somewhere else doesn't get those instructions.
3. **Give it the session loop** as skills: a wrap-up ([`../skills/wrapup/`](../skills/wrapup/SKILL.md)) and,
   optionally, a read-only opener that reads the last handoff and checks each open item live.

A `CLAUDE.md` block to start from (the infrastructure layout; swap "host" for your entity):

```markdown
## The record
- **Canonical:** `~/record` (git). Read its `CLAUDE.md` first: how we work and where new things go.
- **Search it first:** the `merlin` MCP (`search`, `history`, `get_document`, `index_status`).
  Before any change on a host, run `history(entity=<host>)` and read what is recorded there.
- **Cite:** say which document (path + date) an answer came from, and whether you checked it live or took it
  from the record. The record is true when written; verify before stating something as current.
- **Absence:** an empty or weak result is not proof that something doesn't exist or never happened. Rephrase,
  try `exact=true` for literal strings, widen the filters, then say what you searched.
- **Retrieval notes:** when a lookup takes more than one try, note the question as first asked, where the answer
  was, and the query that would have found it. The wrap-up turns these into eval cases.
- **Wrap-up** writes the session back: journal with real output, records, handoff, commit (`/wrapup`).
```

## From the assistant's side

*Written by the Claude Code assistant that works with this setup daily (an infrastructure homelab). Hostnames are
changed.*

I don't remember previous sessions. Everything I know about the machines, what we tried last week, and why
something is set up the way it is comes from the record, and merlin is how I find it without reading the whole
repo. A few habits do most of the work:

- **History before any change.** Before upgrading packages on a mail server this morning, I ran
  `history(entity="mail1")`. The newest entry said, from the day before, that a PHP service had been masked on
  purpose (two webmail apps switched off during a CVE, by decision, with the date to revisit). Without that I'd have
  seen a masked service and an upgrade touching it, and might have "fixed" it. With it, I knew the upgrade was safe,
  left the service masked, and could say the new package carried the CVE fix.
- **Search, then read the document.** A search excerpt is a pointer, not the answer. When a result looks right, I
  open it with `get_document` before acting, because the section above or below often says "superseded" or adds the
  caveat.
- **Weigh by type.** A journal entry tells me what was seen on a date; a host record or runbook tells me how it is
  now. When they disagree I go with the newer, more canonical one and say so. When the record says something is
  "unverified", I treat it as a hypothesis and check it live before building on it.
- **Say where it came from.** "The router's WireGuard endpoint is X (host record, 10-02, not checked live)" is a
  claim someone can check. "The endpoint is X" from memory is how a plausible mistake gets in.
- **Don't trust an empty result.** No hits usually means I asked badly: wrong word, a filter too narrow, a literal
  string that needs `exact=true`. I rephrase before I say something doesn't exist.
- **Note the misses.** When it takes me three queries to find something, I note what I asked and where the answer
  actually was. At wrap-up those become eval cases, so the next session finds it on the first try.
- **Write it back.** At the end I journal what we did with the real command output, update the record that changed,
  mark the old section superseded, and commit. That is what the next session (me, with no memory of this one)
  will read.

It isn't perfect: I still make mistakes, and a record can be wrong. The difference is that a mistake now has a
source and a date I can point to, so it gets found and fixed instead of sounding right.
