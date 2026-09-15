"""The local alert channel, Alertmanager's webhook into it, and the two alerts the phase is
done-when: a circuit-breaker trip and a consensus disagreement, raised by the gateway."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_llm_gateway.breaker import CircuitBreaker
from slas_llm_gateway.consensus import TokenBudget, default_rules
from slas_llm_gateway.gateway import Gateway
from slas_llm_gateway.redaction import default_redactor
from slas_llm_gateway.routing import RoleRouter, Routes
from slas_llm_gateway.structured import VoterUnavailableError
from slas_llm_gateway.vllm import FakeVllm, Message
from slas_observability import metrics, tracing
from slas_observability.alerts import (
    ALERT_WEBHOOK_URL,
    AlertWebhookServer,
    ListAlertChannel,
    LocalAlertChannel,
    alertmanager_config,
    fingerprint,
)

START = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
VOTERS = ["vllm-voter-qwen", "vllm-voter-deepseek", "vllm-voter-kimi"]


class TickingClock:
    def __init__(self) -> None:
        self.current = START

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        self.current += timedelta(**kwargs)


def test_alerts_are_raised_once_deduplicated_acknowledged_and_summarised(tmp_path: Path) -> None:
    clock = TickingClock()
    channel = LocalAlertChannel(tmp_path / "Alerts" / "alerts.json", clock=clock)
    assert channel.sentence() == "No alert is open."
    with tracing.trace("d" * 32):
        first = channel.raise_(
            "circuit_breaker_open",
            "warning",
            "vllm-coder is paused until 09:05 after 2 invalid answers.",
            instance="vllm-coder",
        )
    assert first.trace_id == "d" * 32 and first.open and first.count == 1
    assert first.id == fingerprint("circuit_breaker_open", {"instance": "vllm-coder"})
    clock.advance(minutes=3)
    again = channel.raise_(
        "circuit_breaker_open",
        "warning",
        "vllm-coder is paused until 09:08 after 3 invalid answers.",
        instance="vllm-coder",
    )
    assert again.id == first.id and again.count == 2
    assert again.sentence.endswith("3 invalid answers.") and again.fired_at == START
    other = channel.raise_("consensus_disagreement", "critical", "Voters split.", decision="rca")
    assert len(channel.open()) == 2
    assert channel.sentence() == "2 alerts are open (1 critical); the newest: Voters split."
    assert again.line() == (
        "2026-09-14 09:00 · warning ×2 · vllm-coder is paused until 09:08 after 3 invalid "
        "answers. (open)"
    )

    acknowledged = channel.acknowledge(other.id, by="lee")
    assert not acknowledged.open and acknowledged.acknowledged_by == "lee"
    assert [a.id for a in channel.open()] == [first.id]
    with pytest.raises(KeyError):
        channel.acknowledge(other.id, by="lee")
    clock.advance(hours=5)
    later = channel.raise_("circuit_breaker_open", "warning", "paused again", instance="vllm-coder")
    assert later.count == 1 and len(channel.all()) == 3, "outside the window: a new alert"
    raw = json.loads((tmp_path / "Alerts" / "alerts.json").read_text())
    assert [a["name"] for a in raw] == [
        "circuit_breaker_open",
        "consensus_disagreement",
        "circuit_breaker_open",
    ]


def test_alertmanager_payloads_land_on_the_channel_over_the_webhook(tmp_path: Path) -> None:
    channel = LocalAlertChannel(tmp_path / "alerts.json", clock=TickingClock())
    server = AlertWebhookServer(channel, bind="127.0.0.1:0")
    server.start()
    try:
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "SlasGpuHot", "severity": "critical", "gpu": "0"},
                    "annotations": {"summary": "GPU 0 is at 91 °C."},
                },
                {
                    "status": "firing",
                    "labels": {"alertname": "SlasNoSeverity", "severity": "weird"},
                    "annotations": {"description": "Something without a summary."},
                },
                {"status": "resolved", "labels": {"alertname": "NeverSeen"}},
            ]
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/internal/alerts",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            assert json.loads(response.read()) == {"accepted": 2}
        alerts = channel.open()
        assert [(a.name, a.severity, a.source) for a in alerts] == [
            ("SlasGpuHot", "critical", "alertmanager"),
            ("SlasNoSeverity", "warning", "alertmanager"),
        ]
        assert alerts[0].sentence == "GPU 0 is at 91 °C." and alerts[0].labels == {"gpu": "0"}

        resolved = {
            "alerts": [{"status": "resolved", "labels": {"alertname": "SlasGpuHot", "gpu": "0"}}]
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/internal/alerts",
            data=json.dumps(resolved).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            assert json.loads(response.read()) == {"accepted": 1}
        assert [a.name for a in channel.open()] == ["SlasNoSeverity"]
        assert channel.all()[0].acknowledged_by == "Prometheus (resolved)"

        bad = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/internal/alerts", data=b"not json", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad, timeout=5)  # noqa: S310
        assert exc.value.code == 400
    finally:
        server.stop()
    config = alertmanager_config()
    assert config["receivers"][0]["webhook_configs"][0]["url"] == ALERT_WEBHOOK_URL
    assert config["route"]["receiver"] == "slas-local"


# --- the gateway raises the two alerts the phase needs -------------------------------------------


def build_gateway(
    vllm: FakeVllm, channel: ListAlertChannel, *, daily_tokens: int = 10_000_000
) -> Gateway:
    clock = FakeClock(START, step=timedelta(seconds=0))
    return Gateway(
        client=vllm,
        router=RoleRouter(Routes(roles={"coder": "vllm-coder"}, voters=VOTERS)),
        redactor=default_redactor(),
        breaker=CircuitBreaker(clock, failure_threshold=2),
        rules=default_rules(),
        budget=TokenBudget(clock, daily_tokens=daily_tokens),
        channel=channel,
    )


def vote(verdict: str, reason: str = "fine") -> dict[str, object]:
    return {"voter": "x", "verdict": verdict, "fields": {}, "reason": reason, "confidence": 0.9}


def test_a_circuit_breaker_trip_raises_one_alert_and_flips_the_gauge() -> None:
    metrics.REGISTRY.reset()
    vllm = FakeVllm()
    channel = ListAlertChannel()
    gateway = build_gateway(vllm, channel)
    vllm.take_down("vllm-coder")
    for _ in range(2):
        with pytest.raises(VoterUnavailableError):
            gateway.complete("coder", [Message(role="user", content="hi")])
    assert channel.sentences() == ["vllm-coder is paused until 09:05 after 2 invalid answers."]
    assert channel.alerts[0].name == "circuit_breaker_open"
    assert channel.alerts[0].labels == {"instance": "vllm-coder"}
    assert metrics.REGISTRY.value("slas_breaker_open", instance="vllm-coder") == 1
    assert metrics.REGISTRY.value("slas_breaker_trips_total", instance="vllm-coder") == 1
    # While open, requests are refused without a new alert; the outcome is counted.
    with pytest.raises(VoterUnavailableError):
        gateway.complete("coder", [Message(role="user", content="hi")])
    assert len(channel.alerts) == 1
    assert (
        metrics.REGISTRY.value("slas_gateway_requests_total", role="coder", outcome="breaker_open")
        == 1
    )
    assert (
        metrics.REGISTRY.value("slas_gateway_requests_total", role="coder", outcome="unavailable")
        == 2
    )
    assert (
        metrics.REGISTRY.value(
            "slas_alerts_raised_total", alert="circuit_breaker_open", severity="warning"
        )
        == 1
    )


def test_a_consensus_disagreement_raises_an_alert_and_counts_the_votes() -> None:
    metrics.REGISTRY.reset()
    vllm = FakeVllm()
    channel = ListAlertChannel()
    gateway = build_gateway(vllm, channel)
    vllm.script_json(VOTERS[0], vote("approve"))
    vllm.script_json(VOTERS[1], vote("approve"))
    vllm.script_json(VOTERS[2], vote("reject", "the plan flashes firmware without a baseline"))
    verdict = gateway.cross_check("plan_approval", [Message(role="user", content="plan")])
    assert not verdict.agreed
    assert len(channel.alerts) == 1
    alert = channel.alerts[0]
    assert alert.name == "consensus_disagreement" and alert.labels == {"decision": "plan_approval"}
    assert alert.sentence.startswith("The voters did not agree on plan_approval; a person decides.")
    assert (
        metrics.REGISTRY.value("slas_consensus_disagreements_total", decision="plan_approval") == 1
    )
    assert (
        metrics.REGISTRY.value(
            "slas_consensus_votes_total", decision="plan_approval", verdict="approve"
        )
        == 2
    )
    assert (
        metrics.REGISTRY.value(
            "slas_consensus_votes_total", decision="plan_approval", verdict="reject"
        )
        == 1
    )
    assert metrics.REGISTRY.value("slas_consensus_budget_remaining_tokens") < 10_000_000

    # Unanimous: no alert.
    for voter in VOTERS:
        vllm.script_json(voter, vote("approve"))
    agreed = gateway.cross_check("plan_approval", [Message(role="user", content="plan")])
    assert agreed.agreed and len(channel.alerts) == 1

    # A voter down: the check is degraded and says who did not answer.
    vllm.take_down(VOTERS[2])
    for voter in VOTERS[:2]:
        vllm.script_json(voter, vote("approve"))
    degraded = gateway.cross_check("plan_approval", [Message(role="user", content="plan")])
    assert degraded.unavailable_voters == [VOTERS[2]]
    assert channel.alerts[-1].name == "consensus_degraded"
    assert channel.alerts[-1].sentence == (
        "plan_approval was checked by 2 of 3 voters; vllm-voter-kimi did not answer."
    )
    assert metrics.REGISTRY.value("slas_consensus_degraded_total", decision="plan_approval") == 1


def test_an_exhausted_budget_reaches_the_channel_too() -> None:
    vllm = FakeVllm()
    channel = ListAlertChannel()
    gateway = build_gateway(vllm, channel, daily_tokens=100)
    vllm.script_json(VOTERS[0], vote("approve"))
    verdict = gateway.cross_check("rca_conclusion", [Message(role="user", content="rca")])
    assert verdict.degraded
    assert channel.alerts[0].name == "consensus_budget_exhausted"
    assert "Cross-check budget for today is used up" in channel.alerts[0].sentence
