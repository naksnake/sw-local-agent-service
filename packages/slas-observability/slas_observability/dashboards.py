"""Six Grafana dashboards as code (CLAUDE.md §8.2), rendered to `observability/grafana/`.

    slas-inference          gateway requests, tokens, breaker, budget, vLLM queues and latency
    slas-gpu                DCGM: utilisation, memory, temperature, power, Xid
    slas-agents             tickets by state, steps by outcome, step duration, consensus
    slas-sandboxes-screens  sandboxes and displays open, GUI steps by primitive and outcome
    slas-validation         cycles by outcome, boot failures, runs aborted
    slas-factory            verdicts, held stations, station batches, skill runs

Every panel is a plain sentence of a title, one or two PromQL targets, and a unit. A test
checks that every `slas_…` metric a panel or rule names exists in `metrics.CATALOGUE`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, Final

DATASOURCE_UID: Final = "slas-prometheus"
SCHEMA_VERSION: Final = 39

_DATASOURCE: Final = {"type": "prometheus", "uid": DATASOURCE_UID}


def _target(expr: str, legend: str = "", *, instant: bool = False) -> dict[str, Any]:
    target: dict[str, Any] = {
        "datasource": _DATASOURCE,
        "expr": expr,
        "legendFormat": legend or "__auto",
        "refId": "",
    }
    if instant:
        target["instant"] = True
        target["range"] = False
    return target


def _panel(
    kind: str,
    title: str,
    targets: Sequence[dict[str, Any]],
    *,
    unit: str = "short",
    description: str = "",
    w: int = 12,
    h: int = 8,
    thresholds: Sequence[tuple[str, float | None]] = (("green", None),),
    extra_field: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for i, target in enumerate(targets):
        target["refId"] = chr(ord("A") + i)
    steps = [{"color": color, "value": value} for color, value in thresholds]
    field_config: dict[str, Any] = {
        "defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}},
        "overrides": [],
    }
    if extra_field:
        field_config["defaults"].update(extra_field)
    options: dict[str, Any] = {}
    if kind == "timeseries":
        options = {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        }
    elif kind == "stat":
        options = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "area",
            "textMode": "auto",
        }
    elif kind == "gauge":
        options = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        }
    elif kind == "table":
        options = {"showHeader": True, "cellHeight": "sm"}
    return {
        "type": kind,
        "title": title,
        "description": description,
        "datasource": _DATASOURCE,
        "targets": list(targets),
        "fieldConfig": field_config,
        "options": options,
        "gridPos": {"x": 0, "y": 0, "w": w, "h": h},
    }


def _layout(panels: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pack panels left to right, 24 columns wide, rows as tall as their tallest panel."""
    placed: list[dict[str, Any]] = []
    x = y = row_h = 0
    for i, panel in enumerate(panels):
        w, h = panel["gridPos"]["w"], panel["gridPos"]["h"]
        if x + w > 24:
            x, y, row_h = 0, y + row_h, 0
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        panel["id"] = i + 1
        x += w
        row_h = max(row_h, h)
        placed.append(panel)
    return placed


def _dashboard(
    uid: str, title: str, description: str, panels: Sequence[dict[str, Any]], *, tags: Sequence[str]
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["slas", *tags],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "schemaVersion": SCHEMA_VERSION,
        "version": 1,
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {"text": "Prometheus", "value": DATASOURCE_UID},
                    "hide": 2,
                }
            ]
        },
        "annotations": {"list": []},
        "links": [{"title": "Runbook", "type": "link", "url": "/docs/runbooks/observability.md"}],
        "panels": _layout(panels),
    }


RED_AT_ONE: Final = (("green", None), ("red", 1.0))


def inference() -> dict[str, Any]:
    return _dashboard(
        "slas-inference",
        "SLAS · Inference",
        "The LLM gateway, the Consensus Router and the vLLM instances behind them.",
        [
            _panel(
                "stat",
                "Model instances paused by the breaker",
                [_target("sum(slas_breaker_open)", "paused", instant=True)],
                w=6,
                h=5,
                thresholds=RED_AT_ONE,
                description="1 or more means a role is falling back (§11 tiers).",
            ),
            _panel(
                "stat",
                "Cross-check budget left today",
                [_target("slas_consensus_budget_remaining_tokens", "tokens", instant=True)],
                w=6,
                h=5,
                thresholds=(("red", None), ("orange", 20000.0), ("green", 100000.0)),
            ),
            _panel(
                "stat",
                "Gateway errors, last 15 min",
                [_target("slas:gateway_error_ratio:15m", "errors", instant=True)],
                unit="percentunit",
                w=6,
                h=5,
                thresholds=(("green", None), ("orange", 0.02), ("red", 0.05)),
            ),
            _panel(
                "stat",
                "Consensus agreement, last hour",
                [_target("slas:consensus_agreement_ratio:1h", "agreement", instant=True)],
                unit="percentunit",
                w=6,
                h=5,
                thresholds=(("red", None), ("orange", 0.8), ("green", 0.9)),
            ),
            _panel(
                "timeseries",
                "Gateway requests per second, by outcome",
                [
                    _target(
                        "sum by (outcome) (rate(slas_gateway_requests_total[5m]))", "{{outcome}}"
                    )
                ],
                unit="reqps",
            ),
            _panel(
                "timeseries",
                "Tokens per minute, by role",
                [_target("sum by (role) (rate(slas_gateway_tokens_total[5m])) * 60", "{{role}}")],
            ),
            _panel(
                "timeseries",
                "Breaker trips, per hour",
                [
                    _target(
                        "sum by (instance) (increase(slas_breaker_trips_total[1h]))",
                        "{{instance}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Degraded cross-checks per hour, by decision",
                [
                    _target(
                        "sum by (decision) (increase(slas_consensus_degraded_total[1h]))",
                        "{{decision}}",
                    )
                ],
                description="Fewer voters than the rule asks for: a paused voter or the budget.",
                thresholds=RED_AT_ONE,
            ),
            _panel(
                "timeseries",
                "Votes per hour, by verdict",
                [
                    _target(
                        "sum by (verdict) (increase(slas_consensus_votes_total[1h]))",
                        "{{verdict}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Requests running and waiting on vLLM",
                [
                    _target(
                        "sum by (instance) (vllm:num_requests_running)", "running {{instance}}"
                    ),
                    _target(
                        "sum by (instance) (vllm:num_requests_waiting)", "waiting {{instance}}"
                    ),
                ],
            ),
            _panel(
                "timeseries",
                "KV cache in use",
                [_target("vllm:gpu_cache_usage_perc", "{{instance}}")],
                unit="percentunit",
                thresholds=(("green", None), ("orange", 0.8), ("red", 0.95)),
            ),
            _panel(
                "timeseries",
                "Time to first token, p95",
                [
                    _target(
                        "histogram_quantile(0.95, sum by (le, instance) "
                        "(rate(vllm:time_to_first_token_seconds_bucket[5m])))",
                        "{{instance}}",
                    )
                ],
                unit="s",
            ),
            _panel(
                "timeseries",
                "End-to-end request latency, p95",
                [
                    _target(
                        "histogram_quantile(0.95, sum by (le, instance) "
                        "(rate(vllm:e2e_request_latency_seconds_bucket[5m])))",
                        "{{instance}}",
                    )
                ],
                unit="s",
            ),
        ],
        tags=["inference", "consensus"],
    )


def gpu() -> dict[str, Any]:
    return _dashboard(
        "slas-gpu",
        "SLAS · GPU",
        "Every GPU on the inference host, from the DCGM exporter.",
        [
            _panel(
                "gauge",
                "Utilisation",
                [_target("DCGM_FI_DEV_GPU_UTIL", "gpu {{gpu}}", instant=True)],
                unit="percent",
                w=8,
                h=7,
                extra_field={"min": 0, "max": 100},
            ),
            _panel(
                "gauge",
                "Memory used",
                [_target("slas:gpu_memory_used_ratio", "gpu {{gpu}}", instant=True)],
                unit="percentunit",
                w=8,
                h=7,
                thresholds=(("green", None), ("orange", 0.9), ("red", 0.97)),
                extra_field={"min": 0, "max": 1},
            ),
            _panel(
                "gauge",
                "Temperature",
                [_target("DCGM_FI_DEV_GPU_TEMP", "gpu {{gpu}}", instant=True)],
                unit="celsius",
                w=8,
                h=7,
                thresholds=(("green", None), ("orange", 75.0), ("red", 85.0)),
                extra_field={"min": 0, "max": 100},
            ),
            _panel(
                "timeseries",
                "Utilisation over time",
                [_target("DCGM_FI_DEV_GPU_UTIL", "gpu {{gpu}}")],
                unit="percent",
            ),
            _panel(
                "timeseries",
                "Memory used and free",
                [
                    _target("DCGM_FI_DEV_FB_USED", "used gpu {{gpu}}"),
                    _target("DCGM_FI_DEV_FB_FREE", "free gpu {{gpu}}"),
                ],
                unit="decmbytes",
            ),
            _panel(
                "timeseries",
                "Power",
                [_target("DCGM_FI_DEV_POWER_USAGE", "gpu {{gpu}}")],
                unit="watt",
            ),
            _panel(
                "timeseries",
                "Clocks",
                [
                    _target("DCGM_FI_DEV_SM_CLOCK", "SM gpu {{gpu}}"),
                    _target("DCGM_FI_DEV_MEM_CLOCK", "memory gpu {{gpu}}"),
                ],
                unit="rotmhz",
            ),
            _panel(
                "stat",
                "Xid errors, last 24 h",
                [_target("sum by (gpu) (increase(DCGM_FI_DEV_XID_ERRORS[24h]))", "gpu {{gpu}}")],
                w=12,
                h=6,
                thresholds=RED_AT_ONE,
                description="Any Xid is a hardware or driver fault worth a ticket.",
            ),
            _panel(
                "stat",
                "Host memory available",
                [_target("node_memory_MemAvailable_bytes", "available", instant=True)],
                unit="bytes",
                w=12,
                h=6,
            ),
        ],
        tags=["gpu"],
    )


def agents() -> dict[str, Any]:
    return _dashboard(
        "slas-agents",
        "SLAS · Agents",
        "Tickets, plan steps and cross-checks across the Coding, Validation and Factory agents.",
        [
            _panel(
                "stat",
                "Tickets running",
                [_target('sum(slas_tickets_in_state{state="Running"})', "running", instant=True)],
                w=6,
                h=5,
            ),
            _panel(
                "stat",
                "Waiting for review",
                [
                    _target(
                        'sum(slas_tickets_in_state{state="Needs review"})',
                        "needs review",
                        instant=True,
                    )
                ],
                w=6,
                h=5,
                thresholds=(("green", None), ("orange", 5.0), ("red", 10.0)),
            ),
            _panel(
                "stat",
                "Waiting for approval",
                [_target('sum(slas_tickets_in_state{state="Planned"})', "planned", instant=True)],
                w=6,
                h=5,
                description="Plans with a destructive step wait here for a person (INV-7).",
            ),
            _panel(
                "stat",
                "Step failures, last hour",
                [
                    _target(
                        'sum(increase(slas_agent_turns_total{outcome="failed"}[1h]))',
                        "failed",
                        instant=True,
                    )
                ],
                w=6,
                h=5,
                thresholds=(("green", None), ("orange", 3.0), ("red", 6.0)),
            ),
            _panel(
                "timeseries",
                "Tickets by state",
                [_target("sum by (state) (slas_tickets_in_state)", "{{state}}")],
                extra_field={"custom": {"stacking": {"mode": "normal"}}},
            ),
            _panel(
                "timeseries",
                "Steps per hour, by agent and outcome",
                [
                    _target(
                        "sum by (agent, outcome) (increase(slas_agent_turns_total[1h]))",
                        "{{agent}} {{outcome}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Step duration, p95 by agent",
                [
                    _target(
                        "histogram_quantile(0.95, sum by (le, agent) "
                        "(rate(slas_step_seconds_bucket[30m])))",
                        "{{agent}}",
                    )
                ],
                unit="s",
            ),
            _panel(
                "timeseries",
                "State changes per hour, by state entered",
                [_target("sum by (to) (increase(slas_ticket_state_changes_total[1h]))", "{{to}}")],
            ),
            _panel(
                "timeseries",
                "Cross-check disagreements per hour, by decision",
                [
                    _target(
                        "sum by (decision) (increase(slas_consensus_disagreements_total[1h]))",
                        "{{decision}}",
                    )
                ],
                thresholds=RED_AT_ONE,
            ),
            _panel(
                "timeseries",
                "SOPs exported per hour, by language",
                [_target("sum by (lang) (increase(slas_sop_exports_total[1h]))", "{{lang}}")],
                description="Both lines move together or something is wrong (INV-13).",
            ),
        ],
        tags=["agents", "tickets"],
    )


def sandboxes_screens() -> dict[str, Any]:
    return _dashboard(
        "slas-sandboxes-screens",
        "SLAS · Sandboxes and screens",
        "Code sandboxes, virtual displays and the GUI steps the screen driver performs.",
        [
            _panel(
                "stat",
                "Sandboxes open",
                [_target("slas_sandbox_sessions_open", "open", instant=True)],
                w=8,
                h=5,
                thresholds=(("green", None), ("orange", 6.0), ("red", 8.0)),
            ),
            _panel(
                "stat",
                "Displays open",
                [_target("slas_screen_displays_open", "open", instant=True)],
                w=8,
                h=5,
                thresholds=(("green", None), ("orange", 6.0), ("red", 8.0)),
            ),
            _panel(
                "stat",
                "GUI steps failing, last 15 min",
                [_target("slas:screen_step_failure_ratio:15m", "failing", instant=True)],
                unit="percentunit",
                w=8,
                h=5,
                thresholds=(("green", None), ("orange", 0.1), ("red", 0.2)),
            ),
            _panel(
                "timeseries",
                "Sandboxes and displays over time",
                [
                    _target("slas_sandbox_sessions_open", "sandboxes"),
                    _target("slas_screen_displays_open", "displays"),
                ],
            ),
            _panel(
                "timeseries",
                "GUI steps per minute, by primitive",
                [
                    _target(
                        "sum by (primitive) (rate(slas_screen_steps_total[5m])) * 60",
                        "{{primitive}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "GUI steps per hour, by outcome",
                [
                    _target(
                        "sum by (outcome) (increase(slas_screen_steps_total[1h]))", "{{outcome}}"
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Skill runs per hour, by outcome",
                [_target("sum by (outcome) (increase(slas_skill_runs_total[1h]))", "{{outcome}}")],
            ),
            _panel(
                "table",
                "Skill runs, last 24 h",
                [
                    _target(
                        "sum by (skill, outcome) (increase(slas_skill_runs_total[24h]))",
                        "",
                        instant=True,
                    )
                ],
                w=24,
                h=8,
            ),
        ],
        tags=["sandboxes", "screens", "skills"],
    )


def validation() -> dict[str, Any]:
    return _dashboard(
        "slas-validation",
        "SLAS · Validation runs",
        "Power cycles on lab targets: outcomes, boot failures, guardrail aborts.",
        [
            _panel(
                "stat",
                "Cycles in the last hour",
                [
                    _target(
                        "sum(increase(slas_validation_cycles_total[1h]))", "cycles", instant=True
                    )
                ],
                w=8,
                h=5,
            ),
            _panel(
                "stat",
                "Boot failures in the last hour",
                [
                    _target(
                        'sum(increase(slas_validation_cycles_total{outcome="boot_failed"}[1h]))',
                        "boot failed",
                        instant=True,
                    )
                ],
                w=8,
                h=5,
                thresholds=(("green", None), ("orange", 1.0), ("red", 3.0)),
            ),
            _panel(
                "stat",
                "Runs aborted, last 24 h",
                [
                    _target(
                        'sum(increase(slas_validation_runs_total{outcome="aborted"}[24h]))',
                        "aborted",
                        instant=True,
                    )
                ],
                w=8,
                h=5,
                thresholds=RED_AT_ONE,
            ),
            _panel(
                "timeseries",
                "Cycles per hour, by kind and outcome",
                [
                    _target(
                        "sum by (kind, outcome) (increase(slas_validation_cycles_total[1h]))",
                        "{{kind}} {{outcome}}",
                    )
                ],
                w=24,
                extra_field={"custom": {"stacking": {"mode": "normal"}}},
            ),
            _panel(
                "timeseries",
                "Runs ended per day, by outcome",
                [
                    _target(
                        "sum by (outcome) (increase(slas_validation_runs_total[1d]))",
                        "{{outcome}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Validation tickets by state",
                [
                    _target(
                        'sum by (state) (slas_tickets_in_state{agent="validation"})', "{{state}}"
                    )
                ],
            ),
        ],
        tags=["validation"],
    )


def factory() -> dict[str, Any]:
    return _dashboard(
        "slas-factory",
        "SLAS · Factory",
        "Units through the test loop: verdicts, held stations, station runners.",
        [
            _panel(
                "stat",
                "Units passed, last 24 h",
                [
                    _target(
                        'sum(increase(slas_factory_verdicts_total{verdict="PASS"}[24h]))',
                        "PASS",
                        instant=True,
                    )
                ],
                w=6,
                h=5,
            ),
            _panel(
                "stat",
                "Units failed, last 24 h",
                [
                    _target(
                        'sum(increase(slas_factory_verdicts_total{verdict="FAIL"}[24h]))',
                        "FAIL",
                        instant=True,
                    )
                ],
                w=6,
                h=5,
                thresholds=(("green", None), ("orange", 1.0), ("red", 5.0)),
            ),
            _panel(
                "stat",
                "Stations held for a line lead",
                [_target("slas_factory_stations_held", "held", instant=True)],
                w=6,
                h=5,
                thresholds=RED_AT_ONE,
            ),
            _panel(
                "stat",
                "Line-lead decisions, last 24 h",
                [
                    _target(
                        'sum(increase(slas_factory_verdicts_total{decided_by="line_lead"}[24h]))',
                        "line lead",
                        instant=True,
                    )
                ],
                w=6,
                h=5,
            ),
            _panel(
                "timeseries",
                "Verdicts per hour",
                [
                    _target(
                        "sum by (verdict) (increase(slas_factory_verdicts_total[1h]))",
                        "{{verdict}}",
                    )
                ],
                extra_field={"custom": {"stacking": {"mode": "normal"}}},
            ),
            _panel(
                "timeseries",
                "Batches to stations per hour, by station and success",
                [
                    _target(
                        "sum by (station, ok) (increase(slas_station_batches_total[1h]))",
                        "{{station}} ok={{ok}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Login + BurnIn skill runs per hour, by outcome",
                [
                    _target(
                        "sum by (outcome) (increase(slas_skill_runs_total"
                        '{skill="station-login-burnin"}[1h]))',
                        "{{outcome}}",
                    )
                ],
            ),
            _panel(
                "timeseries",
                "Factory tickets by state",
                [_target('sum by (state) (slas_tickets_in_state{agent="factory"})', "{{state}}")],
            ),
        ],
        tags=["factory"],
    )


DASHBOARDS: Final[dict[str, Any]] = {
    "slas-inference": inference,
    "slas-gpu": gpu,
    "slas-agents": agents,
    "slas-sandboxes-screens": sandboxes_screens,
    "slas-validation": validation,
    "slas-factory": factory,
}


def render_dashboard(uid: str) -> dict[str, Any]:
    dashboard: dict[str, Any] = DASHBOARDS[uid]()
    return dashboard


_METRIC_TOKEN: Final = re.compile(r"(?<![A-Za-z0-9_:])([A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_:]+)?)")
_PROMQL_WORDS: Final = frozenset(
    {
        "sum",
        "by",
        "rate",
        "increase",
        "histogram_quantile",
        "le",
        "instance",
        "clamp_min",
        "on",
        "and",
        "or",
        "unless",
        "without",
        "count",
        "max",
        "min",
        "avg",
        "m",
        "h",
        "d",
        "s",
    }
)


def metric_names_in(expr: str) -> set[str]:
    """Metric names referenced by a PromQL expression (labels inside `{}` are skipped)."""
    stripped = re.sub(r"\{[^}]*\}", "", expr)
    stripped = re.sub(r"\b(by|without)\s*\([^)]*\)", r"\1 ()", stripped)  # label groupings
    stripped = re.sub(r"\[[0-9]+[smhd]\]", "", stripped)
    stripped = re.sub(r'"[^"]*"', "", stripped)
    names: set[str] = set()
    for match in _METRIC_TOKEN.finditer(stripped):
        token = match.group(1)
        if token in _PROMQL_WORDS or token.isdigit():
            continue
        names.add(token)
    return names


def expressions(dashboard: dict[str, Any]) -> list[str]:
    return [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
