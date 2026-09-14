# compose

docker-compose.yml, prod.override.yml, macvlan.override.yml. Conceptual blueprint in CLAUDE.md §12. P1.

| File | Phase | What it is |
|---|---|---|
| `../observability/` | P11 | Prometheus, Alertmanager and Grafana files the base compose mounts read-only: `prometheus/prometheus.yml` and `rules.yml` into `prometheus`, `alertmanager/alertmanager.yml` into `alertmanager`, `grafana/provisioning/` and `grafana/dashboards/` into `grafana` (`/etc/grafana/provisioning`, `/etc/grafana/dashboards`). Rendered from `slas_observability`; see `observability/README.md`. |
| `macvlan.override.yml` | P8 | Puts `validation-executor` on the lab VLAN with its own address (macvlan). Values from `.env`: `SLAS_LAB_IFACE`, `SLAS_LAB_SUBNET`, `SLAS_LAB_GATEWAY`, `SLAS_LAB_EXECUTOR_IP`. Targets send syslog to that address on UDP 5514. The base `docker-compose.yml` arrives with Phase 1. |
