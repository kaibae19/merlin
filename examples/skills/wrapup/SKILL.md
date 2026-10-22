---
name: wrapup
description: End-of-session wrap-up — journal the session with verbatim output, update the canonical records, write the handoff, turn lookups into eval cases, scan for secrets and commit. Use when asked to wrap up, document the day, or close out a session.
---

# Wrap-up

A generic starting point for a merlin-indexed record. Adjust the paths to your layout (`merlin.toml`); the layer
rules ("where a new thing goes") belong in the record's own `CLAUDE.md`. Read that first.

1. **Day file:** `journal/YYYY/MM/<YYYY-MM-DD>.md`, dated by the day the session **started** (local time).
   If it exists, **append** a `## YYYY-MM-DD (session N) — <headline>` block. Never rewrite an earlier session's block.
2. **Journal entry:** a headline, a short **index** (one line per topic), then one `###` per topic with
   `Did:` / `Hit:` / `Decided:` / `Open:`.
   - **Paste the real commands and output** from this session. Trim to the lines that carry evidence; never
     summarize or paraphrase output.
   - **Quote** the person for decisions ("let's leave them off"), with who said it.
   - Cover everything touched, including routine work. Say what was checked live and what came from the record.
3. **Canonical layers**, only where this session produced something:
   - The entity's record (its current state), a how-to (a procedure), a lesson (short, pointer first), the backlog
     (a task), ideas.
   - A section the session made stale: start its first line with `(superseded YYYY-MM-DD: see § X)`. Don't delete it.
4. **Handoff:** one short paragraph at the top of the memory index or a handoff file: what is mid-flight, what is
   next, what to ask. "Nothing mid-flight" is a valid handoff.
5. **Retrieval retrospective:** for each lookup that took more than one try, note the question as first asked, where
   the answer was, and the query that would have found it first. Add the worthwhile ones to the eval cases and run
   the eval.
6. **Secrets, then commit:** `git diff --cached | grep -nEi 'token|passw|secret|api[_-]?key|BEGIN .*PRIVATE'`; read the
   whole output. Never commit a credential: point to where it lives instead. One commit for the session's record
   changes; confirm with `git log -1 --oneline`.
7. **Report:** the files changed, the commit, and anything skipped and why.
