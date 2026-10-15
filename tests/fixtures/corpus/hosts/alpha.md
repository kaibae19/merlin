---
host: alpha
aliases: [alpha.example.internal, nas]
ips: [192.0.2.10]
roles: [storage, nfs]
status: active
---
# alpha

Storage server. Exports NFS shares to beta.

## Facts
<!-- facts:begin — generated; do not edit by hand -->
<!-- facts:end -->

## Notes

### Pool scrub found checksum errors (2030-01-14)
`zpool status` showed 3 CKSUM errors on disk 2. Replaced the SATA cable; the next scrub was clean.

### Undated: NFS export options
Exports use `sec=sys`, `no_subtree_check`.
