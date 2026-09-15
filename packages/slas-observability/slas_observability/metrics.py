"""Application metrics (CLAUDE.md §8.2) in the Prometheus text format, standard library only.

The `CATALOGUE` is the closed set of metric names the platform emits. Code increments by
name (`inc("slas_screen_steps_total", primitive="click", outcome="ok")`); a name or a label
set that is not in the catalogue is a programming error and raises at once, so the dashboards
and alert rules — tested against the same catalogue — never reference a metric that does not
exist. `REGISTRY.render()` is what `/metrics` serves.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

MetricKind = Literal["counter", "gauge", "histogram"]

DEFAULT_BUCKETS: Final[tuple[float, ...]] = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


@dataclass(frozen=True)
class MetricSpec:
    name: str
    kind: MetricKind
    help: str
    labels: tuple[str, ...] = ()
    buckets: tuple[float, ...] = DEFAULT_BUCKETS


def _spec(
    name: str, kind: MetricKind, help_text: str, *labels: str, buckets: Iterable[float] = ()
) -> MetricSpec:
    return MetricSpec(
        name, kind, help_text, tuple(labels), tuple(buckets) if buckets else DEFAULT_BUCKETS
    )


#: Every metric the platform emits. §8.2 names first, then what the dashboards and alerts
#: need beyond them. Labels are closed sets in code, never free text (no ticket ids, no
#: user names: those belong in the journal).
CATALOGUE: Final[dict[str, MetricSpec]] = {
    spec.name: spec
    for spec in (
        # --- CLAUDE.md §8.2 ---------------------------------------------------------------
        _spec(
            "slas_agent_turns_total",
            "counter",
            "Plan steps performed by an agent, by outcome.",
            "agent",
            "outcome",
        ),
        _spec(
            "slas_consensus_votes_total",
            "counter",
            "Votes returned by the Consensus Router, by decision and verdict.",
            "decision",
            "verdict",
        ),
        _spec(
            "slas_consensus_disagreements_total",
            "counter",
            "Cross-checks whose voters did not meet the rule (a person decides).",
            "decision",
        ),
        _spec(
            "slas_skill_runs_total",
            "counter",
            "Skill runs, by skill id and outcome.",
            "skill",
            "outcome",
        ),
        _spec(
            "slas_screen_steps_total",
            "counter",
            "GUI primitives performed by the screen driver, by primitive and outcome.",
            "primitive",
            "outcome",
        ),
        _spec(
            "slas_ticket_state_changes_total",
            "counter",
            "Ticket state transitions, by agent and the state entered.",
            "agent",
            "to",
        ),
        _spec("slas_sop_exports_total", "counter", "SOP files rendered, by language.", "lang"),
        # --- inference and the Consensus Router -----------------------------------------
        _spec(
            "slas_gateway_requests_total",
            "counter",
            "Requests the LLM gateway sent to a model instance, by role and outcome.",
            "role",
            "outcome",
        ),
        _spec(
            "slas_gateway_tokens_total",
            "counter",
            "Tokens the gateway spent, by role and kind (prompt or completion).",
            "role",
            "kind",
        ),
        _spec(
            "slas_breaker_open",
            "gauge",
            "1 while the circuit breaker pauses a model instance, else 0.",
            "instance",
        ),
        _spec(
            "slas_breaker_trips_total",
            "counter",
            "Times the circuit breaker opened for a model instance.",
            "instance",
        ),
        _spec(
            "slas_consensus_degraded_total",
            "counter",
            "Cross-checks answered by fewer voters than the rule asks for.",
            "decision",
        ),
        _spec(
            "slas_consensus_budget_remaining_tokens",
            "gauge",
            "Tokens left in today's cross-check budget.",
        ),
        # --- agents, tickets --------------------------------------------------------------
        _spec(
            "slas_tickets_in_state",
            "gauge",
            "Tickets currently in each state, by agent.",
            "agent",
            "state",
        ),
        _spec(
            "slas_step_seconds",
            "histogram",
            "Wall-clock seconds a plan step took, by agent.",
            "agent",
            buckets=(1, 5, 15, 60, 300, 900, 1800, 3600, 7200),
        ),
        # --- sandboxes and screens --------------------------------------------------------
        _spec("slas_sandbox_sessions_open", "gauge", "Code sandboxes open on this host."),
        _spec("slas_screen_displays_open", "gauge", "Virtual displays open in the screen worker."),
        # --- validation -------------------------------------------------------------------
        _spec(
            "slas_validation_cycles_total",
            "counter",
            "Validation power cycles performed, by kind and outcome.",
            "kind",
            "outcome",
        ),
        _spec(
            "slas_validation_runs_total",
            "counter",
            "Validation runs that ended, by outcome.",
            "outcome",
        ),
        # --- factory ----------------------------------------------------------------------
        _spec(
            "slas_factory_verdicts_total",
            "counter",
            "Factory verdicts, by verdict (PASS, FAIL, line_lead) and who decided.",
            "verdict",
            "decided_by",
        ),
        _spec("slas_factory_stations_held", "gauge", "Stations held for a line lead's decision."),
        _spec(
            "slas_station_batches_total",
            "counter",
            "Signed batches sent to station runners, by station, kind and whether it succeeded.",
            "station",
            "kind",
            "ok",
        ),
        # --- alerts -----------------------------------------------------------------------
        _spec(
            "slas_alerts_raised_total",
            "counter",
            "Alerts raised on the local channel, by alert name and severity.",
            "alert",
            "severity",
        ),
    )
}

METRIC_NAMES: Final[frozenset[str]] = frozenset(CATALOGUE)


class MetricError(ValueError):
    pass


LabelKey = tuple[str, ...]


@dataclass
class _Series:
    """One metric's samples keyed by label values (in the spec's label order)."""

    spec: MetricSpec
    values: dict[LabelKey, float] = field(default_factory=dict)
    # histograms
    sums: dict[LabelKey, float] = field(default_factory=dict)
    counts: dict[LabelKey, int] = field(default_factory=dict)
    buckets: dict[LabelKey, list[int]] = field(default_factory=dict)


class Registry:
    def __init__(self, catalogue: Mapping[str, MetricSpec] = CATALOGUE) -> None:
        self.catalogue = dict(catalogue)
        self._series: dict[str, _Series] = {n: _Series(s) for n, s in self.catalogue.items()}
        self._lock = threading.Lock()

    # --- keys -------------------------------------------------------------------------------

    def _key(self, name: str, labels: Mapping[str, object], kind: MetricKind) -> LabelKey:
        spec = self.catalogue.get(name)
        if spec is None:
            raise MetricError(
                f"{name} is not a metric this platform emits; add it to "
                "slas_observability.metrics.CATALOGUE first."
            )
        if spec.kind != kind:
            raise MetricError(f"{name} is a {spec.kind}, not a {kind}.")
        if set(labels) != set(spec.labels):
            raise MetricError(f"{name} takes the labels {list(spec.labels)}, not {sorted(labels)}.")
        return tuple(str(labels[label]) for label in spec.labels)

    # --- writes -----------------------------------------------------------------------------

    def inc(self, name: str, amount: float = 1.0, **labels: object) -> None:
        if amount < 0:
            raise MetricError(f"{name} is a counter; it only goes up.")
        key = self._key(name, labels, "counter")
        with self._lock:
            series = self._series[name]
            series.values[key] = series.values.get(key, 0.0) + amount

    def set(self, name: str, value: float, **labels: object) -> None:
        key = self._key(name, labels, "gauge")
        with self._lock:
            self._series[name].values[key] = float(value)

    def add(self, name: str, delta: float, **labels: object) -> None:
        """Gauge increment or decrement (never below zero: a count of things cannot be)."""
        key = self._key(name, labels, "gauge")
        with self._lock:
            series = self._series[name]
            series.values[key] = max(0.0, series.values.get(key, 0.0) + delta)

    def observe(self, name: str, value: float, **labels: object) -> None:
        key = self._key(name, labels, "histogram")
        with self._lock:
            series = self._series[name]
            series.sums[key] = series.sums.get(key, 0.0) + value
            series.counts[key] = series.counts.get(key, 0) + 1
            counts = series.buckets.setdefault(key, [0] * len(series.spec.buckets))
            for i, upper in enumerate(series.spec.buckets):
                if value <= upper:
                    counts[i] += 1

    # --- reads ------------------------------------------------------------------------------

    def value(self, name: str, **labels: object) -> float:
        spec = self.catalogue[name]
        key = tuple(str(labels[label]) for label in spec.labels)
        with self._lock:
            series = self._series[name]
            if spec.kind == "histogram":
                return series.sums.get(key, 0.0)
            return series.values.get(key, 0.0)

    def count(self, name: str, **labels: object) -> int:
        spec = self.catalogue[name]
        key = tuple(str(labels[label]) for label in spec.labels)
        with self._lock:
            return self._series[name].counts.get(key, 0)

    def samples(self, name: str) -> dict[LabelKey, float]:
        with self._lock:
            return dict(self._series[name].values)

    def reset(self) -> None:
        with self._lock:
            self._series = {n: _Series(s) for n, s in self.catalogue.items()}

    def render(self) -> str:
        """The Prometheus text exposition format, deterministic (sorted) for tests and diffs."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._series):
                series = self._series[name]
                spec = series.spec
                lines.append(f"# HELP {name} {_escape_help(spec.help)}")
                lines.append(f"# TYPE {name} {spec.kind}")
                if spec.kind == "histogram":
                    for key in sorted(series.counts):
                        base = _labels(spec.labels, key)
                        cumulative = 0
                        for upper, in_bucket in zip(spec.buckets, series.buckets[key], strict=True):
                            cumulative = in_bucket
                            lines.append(
                                f"{name}_bucket{_with(base, 'le', _fmt(upper))} {cumulative}"
                            )
                        lines.append(
                            f"{name}_bucket{_with(base, 'le', '+Inf')} {series.counts[key]}"
                        )
                        lines.append(f"{name}_sum{_wrap(base)} {_fmt(series.sums[key])}")
                        lines.append(f"{name}_count{_wrap(base)} {series.counts[key]}")
                    continue
                for key in sorted(series.values):
                    lines.append(
                        f"{name}{_wrap(_labels(spec.labels, key))} {_fmt(series.values[key])}"
                    )
        return "\n".join(lines) + "\n"


def _fmt(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(names: Sequence[str], values: Sequence[str]) -> list[str]:
    return [f'{n}="{_escape_label(v)}"' for n, v in zip(names, values, strict=True)]


def _wrap(pairs: list[str]) -> str:
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _with(pairs: list[str], name: str, value: str) -> str:
    return _wrap([*pairs, f'{name}="{value}"'])


#: The process-wide registry every service exposes on /metrics.
REGISTRY: Final = Registry()


def inc(name: str, amount: float = 1.0, **labels: object) -> None:
    REGISTRY.inc(name, amount, **labels)


def set_gauge(name: str, value: float, **labels: object) -> None:
    REGISTRY.set(name, value, **labels)


def add_gauge(name: str, delta: float, **labels: object) -> None:
    REGISTRY.add(name, delta, **labels)


def observe(name: str, value: float, **labels: object) -> None:
    REGISTRY.observe(name, value, **labels)


def render() -> str:
    return REGISTRY.render()
