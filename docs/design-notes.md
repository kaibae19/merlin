# Design notes

Why merlin ranks and ingests the way it does. The numbers come from a reference corpus: a personal infrastructure
record in markdown (daily journal, host records, runbooks, short lessons, plans) and a retrieval eval of 23, later 26,
natural-language questions written against it. They show direction and size, not a benchmark; measure on your own
corpus with `merlin.evaluate` before adopting the defaults.

## Measure first

An early ranking change made without measurement made a query worse, so ranking work starts and ends with the eval
(README § Evaluation). Each change below was kept only if the eval improved:

| Step | hit@1 | hit@3 | MRR |
|---|---|---|---|
| Baseline (RRF only) | 57% | 83% | 0.728 |
| + doc-type boost + recency | 70% | 96% | 0.815 |
| (tried, not kept) mild 1.15 / strong 1.5 boosts | 65% / 70% | 96% / 96% | 0.793 / 0.822 |
| + section-level results (stale penalty, near-tie pick, `also`) | 74% | 100% | 0.841 |
| + corpus fixes found with the new warnings, 3 more cases (26) | 81% | 100% | 0.885 |

## Doc-type boost and recency

RRF scores are nearly flat (about 0.016 to 0.03 across a result list), so canonical layers lost to the journal
entries that produced them and to index files that only point at them. A per-type boost and a small recency factor
fixed most of that. A mild 1.15 boost scored worse; a strong 1.5 scored about the same as 1.3. The moderate 1.3 is
the default, to avoid overfitting a small case set. A layout file without `[boost]`/`[recency]` gets neither; the
shipped default layout has both.

## Section-level results

Documents that grow by appending (journals, long runbooks) showed an older, superseded section when a newer one
answered nearly as well, and the reader had no hint that other sections matched. Hence the stale penalty (a marker in
the heading or at the start of the first line, never a passing mention), the near-tie pick (unmarked beats stale, then
newer beats older), and the `also` line naming other matching sections.

A journal's day index restates what its sections say, including superseded ones; `[stale] inherit` (off by default)
lets an overview chunk inherit the stale mark of a matched child section. The near-tie recency compares a section's
own date, never the commit date an undated section inherits, so an undated intro can't beat a dated answer.

## Ingest warnings

- **Unclosed code fence.** One unclosed fence in a long journal file inverted fence parsing for the rest of it: about
  500 lines became one chunk with the wrong heading and every host they mentioned, and it matched almost every query.
  Recovery applies only to unbalanced documents, so balanced ones can still quote markdown headings in code blocks.
- **Invalid front matter.** It used to be ignored silently. The warning found four files on the reference corpus,
  typically an unquoted `: ` in a value, which silently dropped a host record's `aliases:`/`ips:` from host tagging.

## Redaction

Patterns only catch secrets with a recognisable shape; a salted hash list catches every live secret whatever its
shape, so once configured it is required (fail closed). Some corpora are mostly evidence for a pattern: in one larger
infrastructure record every hex32+ token was a checksum, fingerprint, commit ID or image digest, so pattern families
can be switched off. `sk_key` requires an upper-case letter or a 20-character unhyphenated run (hyphenated host names
were being redacted), and `key_value` ignores the sudoers tag `NOPASSWD`.

## Host aliases that are common words

An alias that is also a common word tags every mention of the word: a directory server's DNS alias `ldap` tagged
hundreds of chunks, one per mention of the protocol. `[hosts] text_ignore` keeps such aliases for filters only.

## Fusion and chunk size are corpus properties

On a larger record, RRF fusion with full-text lowered MRR against vector-only, and a 1500-token cap made chunks
several times larger than intended. Both depend on the corpus, so both are layout keys (`[search] fusion`,
`[chunk] max_tokens`).

## Freshness

Flagging every commit as stale the moment it lands, before the next timer run, makes a warning that is almost always
on, so it gets ignored. Hence the 30-minute grace period. A check that cannot read its input (an unreadable HEAD)
reports STALE, never fresh.

## Schema safety

The tables are unqualified, so they land in whatever schema the role's `search_path` names first. Ingest refuses a
schema whose tables are not a merlin index, judges table presence in the connection's own schema only (an empty
pinned schema once resolved to `public` and migrated another index), and creates pgvector in `public`.

## Open notes

- `NEAR_TIE` (0.10) and `ALSO` (2) are module constants in `search.py`, not layout keys.
- The eval has no `[stale]` override; use a copy of the layout via `MERLIN_CONFIG`. `--boost` replaces the boost
  table rather than merging into it.
- The stale `lead` check sees only the first piece of a section split for size; a heading marker applies to every
  piece.
- The stale `heading` check looks at the section's own heading, not its parents.
- For the first section of a document with front matter, the raw front matter is prepended to the chunk body, so the
  `lead` check sees the front matter's first line.
- Recency age uses the container's `date.today()`, which follows `TZ`.
