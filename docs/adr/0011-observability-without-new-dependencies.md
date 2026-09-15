# ADR-0011: Observability — metrics registry, trace ids, local alert channel, provisioned files

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md §8.2 names the application metrics, six Grafana dashboards as code, one trace id
from the WebUI to vLLM and the executor, and alerts; §11 asks for structlog JSON with a
trace id. None of prometheus_client, structlog, OpenTelemetry or PyYAML is an approved
dependency, and the platform must alert without a mail server, chat service or pager outside
the perimeter (INV-1). Phase 11 has to deliver all of §8.2 under those constraints.

## Decision
- **One package, `slas-observability`**, standard library only, depended on by every
  service and by nothing below it (`slas-schemas` is its only dependency). It owns the
  metric catalogue, the Prometheus text exposition, trace propagation, structured events,
  the local alert channel and the rendered Prometheus/Alertmanager/Grafana files.
- **A closed metric catalogue.** Code increments by name; an unknown name or label set
  raises. Dashboards and rules are tested against the same catalogue, so a renamed metric
  fails the build instead of blanking a panel. Labels are closed sets (agents, outcomes,
  primitives, decisions, roles, stations); ticket ids and user names never become labels.
- **W3C trace context, by hand.** The WebUI mints `traceparent`; the api binds the trace id
  to the request context; every hop reads it from the context and forwards `traceparent` and
  `X-Slas-Trace-Id`. Journal entries gain a `trace_id` field; `ExecutionContext` and
  `CompletionRequest` carry it; the vLLM HTTP client sends it. OpenTelemetry, if approved,
  replaces the plumbing behind the same six functions.
- **Events are JSON lines** with `ts`, `level`, `service`, `event`, `trace_id` and fields —
  structlog's shape without structlog.
- **The local alert channel** is a JSON file under `${SLAS_DATA_ROOT}/Alerts/`, written by
  platform code directly (breaker trips, consensus disagreements, degraded checks, budget)
  and by Alertmanager through a webhook on the api. Deduplicated by fingerprint within four
  hours; acknowledged by a person or by Alertmanager's `resolved`. The WebUI Home page and
  `slas status` read it. No external receiver exists by default.
- **Provisioned files are rendered from Python** (`observability/`), including a small YAML
  emitter that quotes anything YAML would misread; a test keeps files and code in step.
- **`slas status`** reads the platform's own files and asks `docker compose ps` and
  `nvidia-smi` through the same read-only `Host` abstraction as `slas doctor`.

## Consequences
- Every service starts a `MetricsServer` on `:8000` of the backend network; Prometheus's
  scrape configuration lists them by compose service name.
- Compose (Phase 1 base file) gains `prometheus`, `alertmanager`, `grafana`, `dcgm-exporter`,
  `node-exporter` and `postgres-exporter` with the `observability/` mounts; images pinned by
  digest like every other (INV-8). Alertmanager is one container more than §12 lists.
- The api gains `POST /internal/alerts` (Alertmanager's receiver) and the Home page shows
  open alerts; both arrive with apps/api.
- Dashboards cannot be edited in Grafana; the source of truth is code.

## Invariants touched
INV-1 (no external receiver, no CDN, files rendered offline), INV-5 (events and alerts
carry sentences and ids, never secrets; the gateway's redactor is the events' `redact`
hook), INV-8 (no new Python dependency), INV-9 (alert rules and dashboards change by
re-rendering files Prometheus and Grafana reload; nothing else restarts).
