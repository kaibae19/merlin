---
host: beta
aliases: [beta.example.internal]
ips: [192.0.2.20]
roles: [docker]
---
# beta

Container host. Mounts its bulk data from the nas.

## Notes

### Disk nearly full (2030-01-15)
Root filesystem at 96 %. Docker image cache was the culprit.
