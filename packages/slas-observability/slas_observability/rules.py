"""Prometheus scrape configuration and alert rules, rendered to `observability/prometheus/`.

Every alert carries three annotations a person can act on — `summary` (what happened),
`likely_cause` and `what_to_do` — in the platform's three-part shape (CLAUDE.md §11).
The two alerts the phase is done-when are `SlasCircuitBreakerOpen` and
`SlasConsensusDisagreement`. Metric names are checked against `metrics.CATALOGUE` by a test.
"""

from __future__ import annotations

from typing import Any, Final

from slas_observability.metrics import CATALOGUE

SERVICES: Final[tuple[str, ...]] = (
    "api",
    "agent-core-orchestrator",
    "llm-gateway",
    "model-manager",
    "sandbox-manager",
    "screen-worker",
    "git-broker",
    "validation-executor",
    "factory-executor",
)

VLLM_ROLES: Final[tuple[str, ...]] = ("coder", "planner", "triage", "embed", "rerank")

#: Metric names that come from other exporters (vLLM, DCGM, node, Prometheus itself).
EXTERNAL_METRICS: Final[frozenset[str]] = frozenset(
    {
        "up",
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
        "vllm:gpu_cache_usage_perc",
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:e2e_request_latency_seconds_bucket",
        "vllm:time_to_first_token_seconds_bucket",
        "vllm:request_success_total",
        "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_GPU_UTIL",
        "DCGM_FI_DEV_FB_USED",
        "DCGM_FI_DEV_FB_FREE",
        "DCGM_FI_DEV_POWER_USAGE",
        "DCGM_FI_DEV_XID_ERRORS",
        "DCGM_FI_DEV_MEM_CLOCK",
        "DCGM_FI_DEV_SM_CLOCK",
        "node_memory_MemAvailable_bytes",
        "node_filesystem_avail_bytes",
    }
)


def prometheus_config() -> dict[str, Any]:
    """`prometheus.yml`: the platform's services, the vLLM instances, DCGM, node, itself."""
    return {
        "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
        "rule_files": ["/etc/prometheus/rules.yml"],
        "alerting": {"alertmanagers": [{"static_configs": [{"targets": ["alertmanager:9093"]}]}]},
        "scrape_configs": [
            {
                "job_name": "slas-services",
                "metrics_path": "/metrics",
                "static_configs": [
                    {"targets": [f"{service}:8000"], "labels": {"service": service}}
                    for service in SERVICES
                ],
            },
            {
                "job_name": "vllm",
                "metrics_path": "/metrics",
                "static_configs": [
                    {"targets": [f"vllm-{role}:8000"], "labels": {"role": role}}
                    for role in VLLM_ROLES
                ],
            },
            {"job_name": "dcgm", "static_configs": [{"targets": ["dcgm-exporter:9400"]}]},
            {"job_name": "node", "static_configs": [{"targets": ["node-exporter:9100"]}]},
            {"job_name": "postgres", "static_configs": [{"targets": ["postgres-exporter:9187"]}]},
            {"job_name": "prometheus", "static_configs": [{"targets": ["localhost:9090"]}]},
        ],
    }


def _alert(
    name: str,
    expr: str,
    *,
    severity: str,
    summary: str,
    likely_cause: str,
    what_to_do: str,
    for_: str = "0m",
) -> dict[str, Any]:
    return {
        "alert": name,
        "expr": expr,
        "for": for_,
        "labels": {"severity": severity},
        "annotations": {
            "summary": summary,
            "likely_cause": likely_cause,
            "what_to_do": what_to_do,
        },
    }


def _record(name: str, expr: str) -> dict[str, Any]:
    return {"record": name, "expr": expr}


def rule_groups() -> list[dict[str, Any]]:
    return [
        {
            "name": "slas-recording",
            "interval": "30s",
            "rules": [
                _record(
                    "slas:consensus_agreement_ratio:1h",
                    "1 - (sum(increase(slas_consensus_disagreements_total[1h])) / "
                    "clamp_min(sum(increase(slas_consensus_votes_total[1h])) / 3, 1))",
                ),
                _record(
                    "slas:screen_step_failure_ratio:15m",
                    'sum(rate(slas_screen_steps_total{outcome!="ok"}[15m])) / '
                    "clamp_min(sum(rate(slas_screen_steps_total[15m])), 0.001)",
                ),
                _record(
                    "slas:gateway_error_ratio:15m",
                    'sum(rate(slas_gateway_requests_total{outcome!="ok"}[15m])) / '
                    "clamp_min(sum(rate(slas_gateway_requests_total[15m])), 0.001)",
                ),
                _record(
                    "slas:gpu_memory_used_ratio",
                    "DCGM_FI_DEV_FB_USED / clamp_min(DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE, 1)",
                ),
            ],
        },
        {
            "name": "slas-inference",
            "rules": [
                _alert(
                    "SlasVllmInstanceDown",
                    'up{job="vllm"} == 0',
                    for_="2m",
                    severity="critical",
                    summary="The model instance {{ $labels.instance }} ({{ $labels.role }}) "
                    "is not answering Prometheus.",
                    likely_cause="The vLLM container stopped, is still loading weights, or "
                    "ran out of GPU memory.",
                    what_to_do="Open Models; if the instance shows as stopped, `slas model "
                    "fit` and start it again. Roles routed to it fall back per §11 tiers.",
                ),
                _alert(
                    "SlasCircuitBreakerOpen",
                    "slas_breaker_open == 1",
                    severity="warning",
                    summary="The circuit breaker paused the model instance {{ $labels.instance }}.",
                    likely_cause="Two answers in a row were invalid or the instance did not "
                    "answer; requests fall back to another instance or a person for the "
                    "cooldown.",
                    what_to_do="Check the instance on the Models page and its GPU on the GPU "
                    "dashboard. The breaker retries by itself after the cooldown.",
                ),
                _alert(
                    "SlasCircuitBreakerFlapping",
                    "increase(slas_breaker_trips_total[1h]) >= 3",
                    severity="critical",
                    summary="The circuit breaker opened {{ $value }} times in an hour for "
                    "{{ $labels.instance }}.",
                    likely_cause="The instance keeps returning invalid JSON or timing out: "
                    "a model that ignores guided decoding, or a GPU that is too small.",
                    what_to_do="Swap the role to a model from the registry that passed the "
                    "eval gates (`slas model swap`), then watch this dashboard.",
                ),
                _alert(
                    "SlasGatewayErrors",
                    "slas:gateway_error_ratio:15m > 0.05",
                    for_="10m",
                    severity="warning",
                    summary="{{ $value | humanizePercentage }} of gateway requests failed in "
                    "the last 15 minutes.",
                    likely_cause="Schema violations, paused instances or vLLM not answering.",
                    what_to_do="Open the Inference dashboard: the outcome panel says which. "
                    "Schema violations point at the model; unavailability at the instance.",
                ),
                _alert(
                    "SlasInferenceQueueDeep",
                    "vllm:num_requests_waiting > 16",
                    for_="10m",
                    severity="warning",
                    summary="{{ $value }} requests are waiting on {{ $labels.instance }}.",
                    likely_cause="More agents than the instance can serve, or a long prompt "
                    "holding the KV cache.",
                    what_to_do="Let it drain, or give the role a second instance from the "
                    "Models page. Do not restart: waiting requests would be lost.",
                ),
            ],
        },
        {
            "name": "slas-consensus",
            "rules": [
                _alert(
                    "SlasConsensusDisagreement",
                    "increase(slas_consensus_disagreements_total[10m]) > 0",
                    severity="warning",
                    summary="The voters did not agree on {{ $labels.decision }}; a person decides.",
                    likely_cause="The evidence is ambiguous, or one voter's judgement "
                    "differs from the others'.",
                    what_to_do="Open the ticket: the votes and each concern are shown as "
                    "sentences. Decide, and note why.",
                ),
                _alert(
                    "SlasConsensusDegraded",
                    "increase(slas_consensus_degraded_total[1h]) > 0",
                    severity="warning",
                    summary="Cross-checks on {{ $labels.decision }} ran with fewer voters "
                    "than the rule asks for.",
                    likely_cause="A voter is paused by the breaker, or today's cross-check "
                    "budget is used up.",
                    what_to_do="Check the breaker and budget panels on the Inference "
                    "dashboard. Decisions taken meanwhile are flagged on their tickets.",
                ),
                _alert(
                    "SlasConsensusBudgetExhausted",
                    "slas_consensus_budget_remaining_tokens == 0",
                    for_="5m",
                    severity="warning",
                    summary="Today's cross-check budget is used up.",
                    likely_cause="More cross-checks than the 5 % daily budget allows.",
                    what_to_do="Nothing is blocked: checks run on one model and are flagged. "
                    "Raise CONSENSUS_TOKEN_BUDGET_PCT only with a reason.",
                ),
            ],
        },
        {
            "name": "slas-gpu",
            "rules": [
                _alert(
                    "SlasGpuHot",
                    "DCGM_FI_DEV_GPU_TEMP > 85",
                    for_="5m",
                    severity="warning",
                    summary="GPU {{ $labels.gpu }} is at {{ $value }} °C.",
                    likely_cause="Sustained load with poor airflow, or a failed fan.",
                    what_to_do="Check the host's fans and inlet temperature; if it keeps "
                    "climbing, drain the role from this GPU on the Models page.",
                ),
                _alert(
                    "SlasGpuMemoryNearlyFull",
                    "slas:gpu_memory_used_ratio > 0.97",
                    for_="10m",
                    severity="warning",
                    summary="GPU {{ $labels.gpu }} memory is {{ $value | humanizePercentage }} "
                    "used.",
                    likely_cause="Two roles co-reside on the GPU, or the context length is "
                    "larger than the fit assumed.",
                    what_to_do="Run `slas model fit` for the roles on this GPU; move one or "
                    "lower its context.",
                ),
                _alert(
                    "SlasGpuXidErrors",
                    "increase(DCGM_FI_DEV_XID_ERRORS[15m]) > 0",
                    severity="critical",
                    summary="GPU {{ $labels.gpu }} reported Xid errors.",
                    likely_cause="A driver fault, a hardware fault, or an application crash "
                    "on the GPU.",
                    what_to_do="Read dmesg on the platform host for the Xid code; the "
                    "instance on this GPU may need a restart. Record it as a ticket.",
                ),
            ],
        },
        {
            "name": "slas-agents",
            "rules": [
                _alert(
                    "SlasStepFailures",
                    'increase(slas_agent_turns_total{outcome="failed"}[30m]) > 5',
                    severity="warning",
                    summary="{{ $value }} plan steps failed for the {{ $labels.agent }} agent "
                    "in 30 minutes.",
                    likely_cause="A target, station or sandbox is misbehaving, or a plan "
                    "relies on something that is down.",
                    what_to_do="Open Tickets filtered on Needs review; each failed step has "
                    "its observation and screenshots.",
                ),
                _alert(
                    "SlasTicketsWaiting",
                    'sum(slas_tickets_in_state{state="Needs review"}) > 10',
                    for_="1h",
                    severity="info",
                    summary="{{ $value }} tickets wait for a person's review.",
                    likely_cause="Reviews are behind the agents.",
                    what_to_do="Open Tickets → Needs review and work the oldest first.",
                ),
            ],
        },
        {
            "name": "slas-sandboxes-screens",
            "rules": [
                _alert(
                    "SlasSandboxesAtCapacity",
                    "slas_sandbox_sessions_open >= 8",
                    for_="10m",
                    severity="warning",
                    summary="{{ $value }} sandboxes are open on this host.",
                    likely_cause="Many coding tasks at once, or sessions past their TTL that "
                    "were not reaped.",
                    what_to_do="Check the Coding page for idle sessions; the reaper closes "
                    "expired ones every minute.",
                ),
                _alert(
                    "SlasDisplaysAtCapacity",
                    "slas_screen_displays_open >= 8",
                    for_="10m",
                    severity="warning",
                    summary="Every virtual display in the screen worker is in use.",
                    likely_cause="Eight GUI sessions at once (DISPLAYS_PER_WORKER).",
                    what_to_do="Wait for a session to end, or add a screen worker in the "
                    "prod profile.",
                ),
                _alert(
                    "SlasScreenStepFailures",
                    "slas:screen_step_failure_ratio:15m > 0.2",
                    for_="15m",
                    severity="warning",
                    summary="{{ $value | humanizePercentage }} of GUI steps failed in the "
                    "last 15 minutes.",
                    likely_cause="Window titles or timing on a station changed, or a "
                    "window stole the focus.",
                    what_to_do="Read the before/after screenshots on the ticket; tune the "
                    "station under Admin → Stations (window matching, settle time).",
                ),
                _alert(
                    "SlasScreenStopped",
                    'increase(slas_screen_steps_total{outcome="stopped"}[15m]) > 0',
                    severity="info",
                    summary="A GUI step was stopped: an operator took over or the focus "
                    "changed unexpectedly.",
                    likely_cause="Take-over from the VNC view, a denied window, or a focus change.",
                    what_to_do="Nothing if an operator took over on purpose; otherwise read "
                    "the step's sentence on the ticket.",
                ),
            ],
        },
        {
            "name": "slas-validation",
            "rules": [
                _alert(
                    "SlasValidationBootFailures",
                    'increase(slas_validation_cycles_total{outcome="boot_failed"}[1h]) >= 3',
                    severity="critical",
                    summary="{{ $value }} boot failures in an hour during "
                    "{{ $labels.kind }} cycling.",
                    likely_cause="The target does not come back after a power action: "
                    "firmware, a device, or the fixture.",
                    what_to_do="The run aborts by itself after three in a row. Look at the "
                    "target's console on the Validation page before arming it again.",
                ),
                _alert(
                    "SlasValidationRunAborted",
                    'increase(slas_validation_runs_total{outcome="aborted"}[1h]) > 0',
                    severity="warning",
                    summary="A validation run was aborted by a guardrail.",
                    likely_cause="Consecutive boot failures or the maximum run time.",
                    what_to_do="Open the run's ticket; the finding names the guardrail.",
                ),
            ],
        },
        {
            "name": "slas-factory",
            "rules": [
                _alert(
                    "SlasFactoryStationHeld",
                    "slas_factory_stations_held > 0",
                    for_="15m",
                    severity="warning",
                    summary="{{ $value }} station(s) have been held for a line lead for "
                    "15 minutes.",
                    likely_cause="A unit failed or the voters split, and nobody has decided.",
                    what_to_do="Open Factory: the held job shows the draft ticket and the "
                    "Decide buttons.",
                ),
                _alert(
                    "SlasFactoryFailRate",
                    'sum(increase(slas_factory_verdicts_total{verdict="FAIL"}[2h])) / '
                    "clamp_min(sum(increase(slas_factory_verdicts_total[2h])), 1) > 0.3",
                    for_="30m",
                    severity="warning",
                    summary="{{ $value | humanizePercentage }} of units failed in the last "
                    "two hours.",
                    likely_cause="A bad lot, a station drifting out of calibration, or a "
                    "test-loop template change.",
                    what_to_do="Compare the stations on the Factory dashboard; if one "
                    "stands out, check its backup and sensors.",
                ),
                _alert(
                    "SlasStationUnreachable",
                    'increase(slas_station_batches_total{ok="false"}[15m]) > 3',
                    severity="warning",
                    summary="Batches to {{ $labels.station }} keep failing.",
                    likely_cause="The station runner is down, its certificate expired, or "
                    "the factory network is cut.",
                    what_to_do="On the station: `slas-station-runner doctor`. On the "
                    "platform: Admin → Stations shows when it was last seen.",
                ),
            ],
        },
    ]


def rules_config() -> dict[str, Any]:
    return {"groups": rule_groups()}


def known_metric_names() -> set[str]:
    """Every name a dashboard or rule may reference: the catalogue (with a histogram's
    `_bucket`, `_sum` and `_count` series), the recording rules, and the other exporters."""
    names: set[str] = set(EXTERNAL_METRICS)
    for spec in CATALOGUE.values():
        names.add(spec.name)
        if spec.kind == "histogram":
            names.update({f"{spec.name}_bucket", f"{spec.name}_sum", f"{spec.name}_count"})
    for group in rule_groups():
        for rule in group["rules"]:
            if "record" in rule:
                names.add(str(rule["record"]))
    return names
