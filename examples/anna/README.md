# Example layout: an Anna record

For the record of an assistant built from the Anna scaffold: a personal agent that keeps its memory as a git repo of
plain Markdown. merlin grew out of Anna's memory engine, so this is the layout it was tuned on. The entity is a
**host** (a machine or device), and Anna's engines call `host_history` before touching one.

```
journal/2030/01/2030-01-14.md    what was done and seen that day, with real output
journal/handoffs/                the headless morning/evening reports (left out of host_history)
hosts/nas.md                     one per machine or device: what it is, how it's set up now, ## Notes
runbooks/backups/README.md       how to do it now
memory/MEMORY.md, memory/*.md    the lessons index and one lesson per file
planning/backlog.md, ideas.md    intent, not done
bot/context/                     what the bot should know without looking it up (rooms, devices, names)
CLAUDE.md, clock/, checks/, senses/   orientation (type doc)
```

## Use it

In `deploy/.env`:

```bash
RECORD_DIR=/home/you/anna-record          # anna-config paths.record prints it
MERLIN_TOML=../examples/anna/merlin.toml
```

Register the server at **user** scope under the name **`merlin`**, so the brain, the bot and the headless runs all see it
(README § Connect Claude Code).

## Host records need front matter

merlin learns host names from the front matter of `hosts/*.md`. Without it, `host_history` returns nothing and search
results carry no host tags. The record template's `hosts/_template.md` has it; a record started before the
template did needs it added by hand:

```markdown
---
host: nas
aliases: [storage, nas.home.arpa]
ips: [192.0.2.20]
---
# nas
```

Use the same name everywhere in the record (journal, checks, senses), and give other names it goes by as aliases. A
name that is also a common word goes in `[entities] text_ignore` so it isn't matched in body text.
