# Example layout: infrastructure (the shipped default)

The layout merlin ships with: a team or homelab running machines. The entity is a **host**, and
`history(entity="db1")` (or `host_history(host="db1")`) returns what has been seen and done on it, newest first.
The config is the default itself: [`merlin.example.toml`](../../merlin.example.toml).

```
journal/2030/01/2030-01-14.md    what was done today, with the real command output
hosts/db1.md                     one per host: what it is and how it is set up now
runbooks/postgres/README.md      how to do it
memory/replica-lag.md            a lesson, short, with a pointer to the detail
planning/backlog.md              intent, not done
skills/                          session procedures for the assistant
```

```markdown
---
host: db1
aliases: [db1.example.internal, primary-db]
ips: [10.0.0.21]
---
# db1
```
