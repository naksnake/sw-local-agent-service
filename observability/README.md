# observability

Everything Prometheus, Alertmanager and Grafana read, rendered from
`packages/slas-observability` (CLAUDE.md §8.2, P11). Do not edit these files by hand: change
the Python, run `uv run python -m slas_observability.render`, and the test
`tests/unit/test_observability_files_in_step.py` confirms file and code agree.

| Path | Mounted at | What it is |
|---|---|---|
| `prometheus/prometheus.yml` | prometheus `/etc/prometheus/prometheus.yml` | Scrape jobs: every platform service on `:8000/metrics`, the vLLM instances, DCGM, node and postgres exporters; the rule file; Alertmanager. |
| `prometheus/rules.yml` | prometheus `/etc/prometheus/rules.yml` | Recording rules and 19 alerts, each with `summary`, `likely_cause` and `what_to_do`. `SlasCircuitBreakerOpen` and `SlasConsensusDisagreement` are the two the phase is done-when. |
| `alertmanager/alertmanager.yml` | alertmanager `/etc/alertmanager/alertmanager.yml` | One receiver, `slas-local`: a webhook to the api on the backend network, which writes the local alert channel. Nothing leaves the host (INV-1). |
| `grafana/provisioning/datasources/prometheus.yml` | grafana `/etc/grafana/provisioning/datasources/` | The Prometheus datasource, uid `slas-prometheus`, not editable. |
| `grafana/provisioning/dashboards/slas.yml` | grafana `/etc/grafana/provisioning/dashboards/` | The file provider for the folder "SW Local Agent Service". |
| `grafana/dashboards/*.json` | grafana `/etc/grafana/dashboards/` | The six dashboards: `slas-inference`, `slas-gpu`, `slas-agents`, `slas-sandboxes-screens`, `slas-validation`, `slas-factory`. |

The base compose file (Phase 1) mounts these read-only into the `prometheus`, `alertmanager`
and `grafana` services on the `slas-observability` network; `compose/README.md` lists the
mounts. The runbook is `docs/runbooks/observability.md`.
