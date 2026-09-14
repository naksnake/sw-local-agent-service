# compose

docker-compose.yml, prod.override.yml, macvlan.override.yml. Conceptual blueprint in CLAUDE.md §12. P1.

| File | Phase | What it is |
|---|---|---|
| `macvlan.override.yml` | P8 | Puts `validation-executor` on the lab VLAN with its own address (macvlan). Values from `.env`: `SLAS_LAB_IFACE`, `SLAS_LAB_SUBNET`, `SLAS_LAB_GATEWAY`, `SLAS_LAB_EXECUTOR_IP`. Targets send syslog to that address on UDP 5514. The base `docker-compose.yml` arrives with Phase 1. |
