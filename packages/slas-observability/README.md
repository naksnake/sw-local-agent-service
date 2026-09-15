# slas-observability

Metrics, one trace id end to end, structured events, the local alert channel, and the
Prometheus / Alertmanager / Grafana files rendered from code (CLAUDE.md §8.2, P11). Standard
library only; every service depends on it and nothing here depends on a service.

| Module | Owns |
|---|---|
| `metrics.py` | `CATALOGUE` — the closed set of metric names (the §8.2 list first) with kinds and label names; `Registry` with `inc`, `set`, `add`, `observe` and the Prometheus text `render()`; the process-wide `REGISTRY`. An unknown name or label set raises, so dashboards and rules can never reference a metric that does not exist. |
| `exposition.py` | `MetricsServer`: `GET /metrics` and `GET /health` for a service. |
| `tracing.py` | W3C `traceparent` in, `traceparent` + `X-Slas-Trace-Id` out; `trace()` binds an id to the context; `current_trace_id()` is what the journal, events, the gateway and the executors stamp. |
| `events.py` | `EventLog(service, sink)`: one JSON object per line — `ts`, `level`, `service`, `event`, `trace_id`, fields. structlog-shaped without structlog. |
| `alerts.py` | `LocalAlertChannel` (`${SLAS_DATA_ROOT}/Alerts/alerts.json`): raise, dedupe, acknowledge, sentence; `AlertWebhookServer` for Alertmanager's webhook; the Alertmanager configuration. |
| `rules.py` | The Prometheus scrape configuration and the recording and alert rules, each alert with `summary`, `likely_cause`, `what_to_do`. |
| `dashboards.py` | Six Grafana dashboards: inference, GPU, agents, sandboxes and screens, validation runs, factory. |
| `provisioning.py`, `render.py` | Every file under `observability/`; `uv run python -m slas_observability.render` regenerates them and a test keeps them in step. |
| `yamlish.py` | The deterministic YAML emitter for those files (PyYAML is not an approved dependency). |

`slas status` (in `slas-cli`) reads the same alert channel and the platform's own files;
the runbook is `docs/runbooks/observability.md`.
