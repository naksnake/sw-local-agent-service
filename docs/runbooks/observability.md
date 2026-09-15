# Watching the platform: metrics, dashboards, alerts, traces, `slas status`

Everything here runs on the platform host with no route out (INV-1): Prometheus scrapes the
services, Grafana shows six dashboards, Alertmanager delivers to a **local channel** the
WebUI and `slas status` read, and one **trace id** ties a request together from the browser
to the executor.

## 1 · One page: `slas status`

```
slas status            # sentences
slas status --json     # the same as a document, for scripts
```

Six lines and a summary: Services (docker compose), GPUs (nvidia-smi), Models (roles and
voters from `Models/models.yaml`), Work (tickets in progress and waiting for review), Leases
(targets and stations held), Alerts (open on the local channel). Exit code 1 when something
needs a person: a service not running, an open alert, or a data root that cannot be read.
It reads the platform's own files and asks `docker compose ps` and `nvidia-smi`; it changes
nothing. `--compose-file` points at the compose file when it is not under `$SLAS_HOME`.

## 2 · Metrics

Every service exposes `GET /metrics` on port 8000 of the backend network (Prometheus text
format) and `GET /health`. The names are the closed set in
`slas_observability.metrics.CATALOGUE`; the §8.2 list first:

| Metric | Labels | Where it moves |
|---|---|---|
| `slas_agent_turns_total` | agent, outcome | the kernel, after every plan step |
| `slas_consensus_votes_total` | decision, verdict | the gateway, per vote |
| `slas_consensus_disagreements_total` | decision | the gateway, when a rule is not met |
| `slas_skill_runs_total` | skill, outcome | the skill runner |
| `slas_screen_steps_total` | primitive, outcome | the screen driver (ok · failed · stopped) |
| `slas_ticket_state_changes_total` | agent, to | the kernel, per transition |
| `slas_sop_exports_total` | lang | the SOP renderer (both languages move together, INV-13) |

Plus the breaker (`slas_breaker_open`, `slas_breaker_trips_total`), gateway requests and
tokens, the cross-check budget, tickets per state, step duration, sandboxes and displays
open, validation cycles and runs, factory verdicts, held stations and station batches. A name
or label set outside the catalogue raises in code, so a dashboard can never show a metric
that does not exist.

## 3 · Dashboards

Grafana at the edge's `/grafana/`, folder **SW Local Agent Service**, provisioned from
`observability/grafana/dashboards/`:

| Dashboard | Shows |
|---|---|
| SLAS · Inference | breaker, budget, gateway errors, agreement; requests, tokens, trips, votes; vLLM queues, KV cache, latency |
| SLAS · GPU | utilisation, memory, temperature, power, clocks, Xid errors, host memory (DCGM and node exporters) |
| SLAS · Agents | tickets by state, steps by agent and outcome, step duration, state changes, disagreements, SOP exports |
| SLAS · Sandboxes and screens | sandboxes and displays open, GUI steps by primitive and outcome, skill runs |
| SLAS · Validation runs | cycles by kind and outcome, boot failures, runs aborted, validation tickets |
| SLAS · Factory | PASS/FAIL per day and hour, held stations, line-lead decisions, station batches, the login skill |

Dashboards are not editable in the UI; change `slas_observability/dashboards.py` and render.
A real run populates them as soon as Prometheus scrapes the services; `tests/` proves every
panel's PromQL names a metric that exists.

## 4 · Alerts and the local channel

Prometheus evaluates `observability/prometheus/rules.yml`; Alertmanager groups and posts to
`http://api:8000/internal/alerts` on the backend network; the api appends to the local
channel, `${SLAS_DATA_ROOT}/Alerts/alerts.json`. Platform code raises on the same channel
without waiting for a scrape: the gateway raises **circuit_breaker_open** the moment a
breaker trips and **consensus_disagreement** the moment a rule is not met, plus
consensus_degraded and consensus_budget_exhausted. Every alert has three parts — what
happened, likely cause, what to do — and the same alert firing again within four hours bumps
a count instead of adding a line.

Where to see them: the Home page, `slas status` (Alerts line, and the exit code), or the
file. Acknowledge from the Home page or by `slas_observability.alerts.LocalAlertChannel
.acknowledge`; Alertmanager's `resolved` acknowledges its own.

| Alert | Fires when | Severity |
|---|---|---|
| SlasCircuitBreakerOpen | `slas_breaker_open == 1` | warning |
| SlasCircuitBreakerFlapping | 3 trips in an hour | critical |
| SlasConsensusDisagreement | any disagreement in 10 min | warning |
| SlasConsensusDegraded, SlasConsensusBudgetExhausted | fewer voters / budget at 0 | warning |
| SlasVllmInstanceDown | `up{job="vllm"} == 0` for 2 min | critical |
| SlasGpuHot, SlasGpuMemoryNearlyFull, SlasGpuXidErrors | > 85 °C · > 97 % · any Xid | warning / warning / critical |
| SlasStepFailures, SlasTicketsWaiting | > 5 failed steps in 30 min · > 10 waiting for an hour | warning / info |
| SlasSandboxesAtCapacity, SlasDisplaysAtCapacity, SlasScreenStepFailures, SlasScreenStopped | 8 open · 8 open · > 20 % failing · a stop | warning / warning / warning / info |
| SlasValidationBootFailures, SlasValidationRunAborted | 3 boot failures in an hour · a guardrail abort | critical / warning |
| SlasFactoryStationHeld, SlasFactoryFailRate, SlasStationUnreachable | held 15 min · > 30 % FAIL in 2 h · batches failing | warning |

A local mail relay or chat webhook can be added as a second receiver in
`slas_observability.alerts.alertmanager_config()` once one exists inside the perimeter.

## 5 · One trace id

The WebUI mints a W3C `traceparent` for every request (`apps/webui/src/trace.ts`). The api
accepts it or mints one (`slas_observability.tracing.accept`) and binds it to the request;
from there every hop reads `current_trace_id()`: the kernel stamps it on every journal entry
and on the executor's context, the gateway puts it on every vLLM request and the HTTP client
sends it as `traceparent` and `X-Slas-Trace-Id`, and every structured event carries it.

To follow one run: take the id from the ticket's journal (`Tickets/<id>/journal.jsonl`,
field `trace_id`), then

```
grep -r <trace_id> ${SLAS_DATA_ROOT}/Tickets ${SLAS_DATA_ROOT}/Alerts   # journal, alerts
docker compose logs api agent-core-orchestrator llm-gateway | grep <trace_id>   # events
```

The test `tests/unit/test_trace_end_to_end.py` starts from a header the WebUI module produces,
runs the api edge, the kernel, the real gateway over HTTP to a fake vLLM that records the
headers it received, and the executor, and asserts that exactly one id appears in every sink.
Tempo (prod profile, P12) can consume the same `traceparent` without a code change.

## 6 · Waiting on decisions

| Blocked | Why | Until then |
|---|---|---|
| Live dashboards from a real run | apps/api, the compose base file and vLLM are not in this environment | the panels are tested against the metric catalogue and the rendered files |
| prometheus_client, structlog, OpenTelemetry | not approved dependencies | the standard-library registry, events and trace plumbing here, same wire formats |
| Loki and Tempo | prod profile (P12) | `docker compose logs` and the journal, both carrying the trace id |
