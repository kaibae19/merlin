# Example layout: a software project

For coming up to speed on a codebase, or for keeping a long-running project's memory straight between sessions.
The entity is a **component**, so `history(entity="billing")` returns everything seen and decided about billing,
newest first.

```
journal/2030/01/2030-01-14.md    what was tried today: commands, real output, errors, who said what (quoted)
components/billing.md            one per component: what it does, interfaces, how it is built now
decisions/0007-queue-choice.md   why it is the way it is (ADR style), dated
howto/release.md                 how to build, release, debug
lessons/flaky-ci-cache.md        a gotcha, short, with a pointer to the journal day that found it
planning/backlog.md              intent, not done
```

A component record names itself and its aliases in front matter. A name also matches inside a path, so a journal
line that mentions `src/billing/invoice.py` is tagged with billing; give a directory with another name as an alias:

```markdown
---
component: billing
aliases: [invoicing, billing-svc, billing-worker]
---
# billing
```

**Learning a new codebase with this:** have the assistant write the component records and lessons from the code,
citing `file:line` for every claim, so each one can be checked against the source instead of trusted. When it later
changes the code, the journal entry and an updated component record are the supplement to the lessons.

Use it with `MERLIN_CONFIG=examples/software-project/merlin.toml` (or copy it to `merlin.toml`).
