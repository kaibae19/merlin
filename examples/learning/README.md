# Example layout: learning a subject

For working through a subject with an assistant (a language, a field, a codebase as curriculum) without picking up
confident-sounding mistakes. The entity is a **topic**, so `history(entity="ownership")` returns everything read,
tried, taught and still open about it, newest first.

```
curriculum.md                    the plan: topics in order (intent, not progress)
journal/2030/01/2030-01-14.md    what was read or tried today, questions asked, answers quoted with their source
topics/ownership.md              current understanding, with the sources that back each point
lessons/ownership-01.md          an explanation written to teach, citing its source for every claim
questions/borrow-vs-move.md      what is not settled yet, and misconceptions found (mark them "wrong: …")
sources/the-book.md              what a source says and where (chapter, page, file:line)
```

```markdown
---
topic: ownership
aliases: [borrowing, borrow checker]
---
# ownership
```

The weighting tells the assistant that a lesson is only as good as the source it cites, and that `questions/` is
what is **not** known. A section headed or opening with "wrong" ranks lower, so a corrected misconception stops
surfacing as the answer but stays in the record.

Use it with `MERLIN_CONFIG=examples/learning/merlin.toml` (or copy it to `merlin.toml`).
