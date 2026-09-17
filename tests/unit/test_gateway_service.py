"""The llm-gateway as a service (docs/api-contract-round-2.md §2): every route against the
fake vLLM, the 503 before the model manager has announced an instance, the instance table
and the httpx client that resolves URLs from it, the YAML loaders, the `slas-gateway` CLI
and the `HttpGateway` client round trip."""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import Field

from slas_http.errors import ServiceError
from slas_kernel.clock import FakeClock
from slas_llm_gateway import cli
from slas_llm_gateway.breaker import BreakerState
from slas_llm_gateway.client import GatewayLike, HttpGateway
from slas_llm_gateway.gateway import Gateway
from slas_llm_gateway.instances import InstanceAnnouncement, InstanceTable, not_registered
from slas_llm_gateway.schema_check import SchemaMismatchError, check, validate_json
from slas_llm_gateway.service.app import (
    HEALTHY_STATES,
    _install_health,
    create_app,
    default_routes,
    route_table,
)
from slas_llm_gateway.service.settings import (
    Settings,
    SettingsError,
    load_consensus_rules,
    load_redactor,
)
from slas_llm_gateway.vllm import (
    CompletionRequest,
    FakeVllm,
    HttpxVllmClient,
    InstanceUnavailableError,
    Message,
    VllmClient,
)
from slas_observability import tracing
from slas_observability.events import EventLog, ListSink
from slas_schemas.common import SlasModel
from slas_schemas.vote import ConsensusVerdict

REPO_ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
VOTERS = ["vllm-voter-qwen", "vllm-voter-deepseek", "vllm-voter-kimi"]
USER = [{"role": "user", "content": "Write hello"}]


def vote_json(verdict: str = "approve", reason: str = "fine") -> dict[str, object]:
    return {"voter": "self", "verdict": verdict, "fields": {}, "reason": reason, "confidence": 0.9}


def announce(name: str, *, healthy: bool = True, model_id: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "url": f"http://{name}:8000",
        "model_id": model_id or f"model-of-{name}",
        "healthy": healthy,
    }


def put_body(
    instances: list[dict[str, Any]],
    roles: dict[str, str] | None = None,
    voters: list[str] | None = None,
) -> dict[str, Any]:
    routes = {
        "roles": roles if roles is not None else {"coder": "vllm-coder", "triage": "vllm-triage"},
        "voters": voters if voters is not None else [],
    }
    return {"instances": instances, "routes": routes}


def assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"what_happened", "likely_cause", "what_to_do", "trace_id"}
    assert all(body[key].strip() for key in ("what_happened", "likely_cause", "what_to_do"))
    return body


class Harness:
    def __init__(self, tmp_path: Path, client: VllmClient | None = None) -> None:
        self.clock = FakeClock(START, step=timedelta(seconds=0))
        self.sink = ListSink()
        self.log = EventLog("llm-gateway", self.sink)
        self.table = InstanceTable(self.clock)
        self.settings = Settings(
            consensus_file=tmp_path / "missing-consensus.yaml",
            redaction_file=tmp_path / "missing-redaction.yaml",
            daily_tokens=1_000_000,
        )
        self.app: FastAPI = create_app(
            settings=self.settings,
            instances=self.table,
            clock=self.clock,
            log=self.log,
            client=client,
        )
        self.client = TestClient(self.app)
        self.gateway: Gateway = self.app.state.gateway

    def records(self, event: str) -> list[dict[str, Any]]:
        return [r for r in self.sink.records() if r["event"] == event]

    def put(self, body: dict[str, Any]) -> httpx.Response:
        response: httpx.Response = self.client.put("/v1/instances", json=body)
        return response


@pytest.fixture(autouse=True)
def _one_trace_per_test() -> Iterator[None]:
    """A bound trace id, as the orchestrator has inside a request; `outbound_headers()`
    would otherwise mint and bind one in this thread and leak it into later tests."""
    with tracing.trace():
        yield


@pytest.fixture
def vllm() -> FakeVllm:
    return FakeVllm()


@pytest.fixture
def harness(tmp_path: Path, vllm: FakeVllm) -> Harness:
    return Harness(tmp_path, client=vllm)


# --- the route table and health -------------------------------------------------------------


def test_route_table_matches_the_contract_section_2(harness: Harness) -> None:
    contract = (REPO_ROOT / "docs" / "api-contract-round-2.md").read_text(encoding="utf-8")
    section = contract.split("## 2. llm-gateway", 1)[1].split("\n## 3.", 1)[0]
    documented = set(re.findall(r"`(GET|POST|PUT|DELETE) (/[^\s`]+)`", section))
    assert documented, "the contract names the gateway's routes in backticks"
    documented |= {("GET", "/health"), ("GET", "/metrics")}
    assert set(route_table(harness.app)) == documented
    assert route_table(harness.app) == sorted(route_table(harness.app))
    assert route_table() == route_table(harness.app), "the default app serves the same table"


def test_health_is_200_with_an_empty_table_and_ok_once_instances_arrive(
    harness: Harness,
) -> None:
    response = harness.client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"service": "llm-gateway", "ok": True, "checks": {"routes": "empty"}}
    assert harness.put(put_body([announce("vllm-coder")])).status_code == 200
    assert harness.client.get("/health").json()["checks"] == {"routes": "ok"}
    assert harness.client.get("/metrics").status_code == 200
    assert {"ok", "empty"} == HEALTHY_STATES


def test_a_state_outside_the_healthy_set_is_a_503(harness: Harness) -> None:
    _install_health(harness.app, lambda: {"routes": "down"})
    response = harness.client.get("/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False and response.json()["checks"] == {"routes": "down"}


# --- before the model manager has spoken ----------------------------------------------------


def test_completion_before_any_instance_is_a_503_that_names_the_role(harness: Harness) -> None:
    body = assert_problem(
        harness.client.post("/v1/complete", json={"role": "coder", "messages": USER}), 503
    )
    assert body["what_happened"] == "No instance serves the role coder yet."
    assert "model manager is still starting vllm-coder" in body["likely_cause"]
    assert "Models page" in body["what_to_do"]
    assert harness.gateway.breaker.failures("vllm-coder") == 0, "absence is not a bad answer"
    generate = harness.client.post(
        "/v1/generate", json={"role": "triage", "messages": USER, "schema": {"type": "object"}}
    )
    assert (
        assert_problem(generate, 503)["what_happened"] == "No instance serves the role triage yet."
    )


def test_default_routes_map_every_role_to_vllm_role_and_have_no_voters(harness: Harness) -> None:
    routes = harness.client.get("/v1/routes").json()
    assert routes == {
        "roles": {
            "coder": "vllm-coder",
            "embed": "vllm-embed",
            "planner": "vllm-planner",
            "rerank": "vllm-rerank",
            "triage": "vllm-triage",
        },
        "voters": [],
        "instances": {},
    }
    assert default_routes().voters == []


def test_an_unknown_role_is_a_503_from_the_router(harness: Harness) -> None:
    harness.put(put_body([announce("vllm-coder")], roles={"coder": "vllm-coder"}))
    body = assert_problem(
        harness.client.post("/v1/complete", json={"role": "planner", "messages": USER}), 503
    )
    assert body["what_happened"] == "No model is serving the planner role."


# --- PUT /v1/instances -----------------------------------------------------------------------


def test_put_instances_replaces_the_table_and_the_routes(harness: Harness) -> None:
    first = harness.put(
        put_body(
            [announce("vllm-coder"), announce("vllm-old", healthy=False)],
            roles={"coder": "vllm-coder"},
            voters=VOTERS[:2],
        )
    )
    assert first.status_code == 200
    answer = first.json()
    assert answer["roles"] == {"coder": "vllm-coder"} and answer["voters"] == VOTERS[:2]
    assert answer["instances"] == {
        "vllm-coder": {
            "url": "http://vllm-coder:8000",
            "model_id": "model-of-vllm-coder",
            "healthy": True,
            "last_seen": START.isoformat(),
        },
        "vllm-old": {
            "url": "http://vllm-old:8000",
            "model_id": "model-of-vllm-old",
            "healthy": False,
            "last_seen": START.isoformat(),
        },
    }
    assert harness.client.get("/v1/routes").json() == answer

    second = harness.put(put_body([announce("vllm-triage")], roles={"triage": "vllm-triage"}))
    assert list(second.json()["instances"]) == ["vllm-triage"], "the table is replaced, not merged"
    assert second.json()["roles"] == {"triage": "vllm-triage"}
    logged = harness.records("instances.replaced")
    assert len(logged) == 2
    assert logged[1]["instances"] == ["vllm-triage"] and logged[1]["roles"] == {
        "triage": "vllm-triage"
    }


def test_put_without_routes_keeps_the_current_routes(harness: Harness) -> None:
    harness.put(put_body([announce("vllm-coder")], roles={"coder": "vllm-coder"}, voters=VOTERS))
    answer = harness.put({"instances": [announce("vllm-coder"), announce(VOTERS[0])]}).json()
    assert answer["roles"] == {"coder": "vllm-coder"} and answer["voters"] == VOTERS
    assert sorted(answer["instances"]) == ["vllm-coder", VOTERS[0]]


def test_put_refuses_unknown_roles_and_bad_urls_in_three_parts(harness: Harness) -> None:
    body = assert_problem(harness.put(put_body([announce("vllm-coder")], roles={"dj": "x"})), 400)
    assert body["what_happened"].startswith("The request couldn't be read: routes")
    assert "unknown roles: dj" in body["what_happened"]
    bad_url = {"instances": [{"name": "vllm-coder", "url": "vllm-coder:8000", "model_id": "m"}]}
    body = assert_problem(harness.put(bad_url), 400)
    assert "instances.0.url" in body["what_happened"]
    assert harness.client.get("/v1/routes").json()["instances"] == {}, (
        "a refused PUT changes nothing"
    )


def test_an_instance_that_becomes_healthy_starts_with_a_closed_breaker(harness: Harness) -> None:
    breaker = harness.gateway.breaker
    breaker.record_failure(VOTERS[0], "did not answer")
    breaker.record_failure(VOTERS[0], "did not answer")
    assert breaker.state(VOTERS[0]) is BreakerState.OPEN
    harness.put(put_body([announce(VOTERS[0])], voters=[VOTERS[0]]))
    assert breaker.state(VOTERS[0]) is BreakerState.CLOSED
    # The same instance announced again while already healthy is not reset.
    breaker.record_failure(VOTERS[0], "schema")
    harness.put(put_body([announce(VOTERS[0])], voters=[VOTERS[0]]))
    assert breaker.failures(VOTERS[0]) == 1


# --- completions through the fake -----------------------------------------------------------


def test_complete_routes_by_role_redacts_and_passes_guided_json(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(put_body([announce("vllm-coder")], roles={"coder": "vllm-coder"}))
    vllm.script("vllm-coder", "print('hi')")
    response = harness.client.post(
        "/v1/complete",
        json={
            "role": "coder",
            "messages": [{"role": "user", "content": "token=abcdef123456 write hi"}],
            "max_tokens": 64,
            "temperature": 0.2,
            "guided_json": {"type": "string"},
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "instance": "vllm-coder",
        "text": "print('hi')",
        "prompt_tokens": response.json()["prompt_tokens"],
        "completion_tokens": 3,
        "finish_reason": "stop",
    }
    sent = vllm.requests[0]
    assert sent.messages[0].content == "token=[redacted:password_assignment] write hi"
    assert sent.max_tokens == 64 and sent.temperature == 0.2
    assert sent.guided_json == {"type": "string"}
    assert response.headers["X-Slas-Trace-Id"]


def test_complete_with_an_instance_that_stops_answering_is_a_503(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(put_body([announce("vllm-coder")], roles={"coder": "vllm-coder"}))
    vllm.take_down("vllm-coder")
    body = assert_problem(
        harness.client.post("/v1/complete", json={"role": "coder", "messages": USER}), 503
    )
    assert body["what_happened"] == "vllm-coder cannot be asked right now."
    assert body["likely_cause"] == "The model instance vllm-coder did not answer."


def test_a_malformed_body_is_a_400_in_three_parts(harness: Harness) -> None:
    body = assert_problem(
        harness.client.post("/v1/complete", json={"role": "coder", "messages": []}), 400
    )
    assert body["what_happened"].startswith("The request couldn't be read: messages")
    assert "Traceback" not in json.dumps(body)


# --- POST /v1/generate ------------------------------------------------------------------------


class Answer(SlasModel):
    owner: str = Field(min_length=1)
    severity: str = Field(pattern=r"^S[1-4]$")


def generate_body(**extra: Any) -> dict[str, Any]:
    return {
        "role": "triage",
        "messages": [{"role": "user", "content": "Who owns it?"}],
        "schema": Answer.model_json_schema(),
        **extra,
    }


def test_generate_returns_the_object_the_tier_and_the_response(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(put_body([announce("vllm-triage")], roles={"triage": "vllm-triage"}))
    vllm.script_json("vllm-triage", {"owner": "EE", "severity": "S2"})
    response = harness.client.post("/v1/generate", json=generate_body(max_tokens=200))
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["object"] == {"owner": "EE", "severity": "S2"} and answer["tier"] == 0
    assert answer["response"]["instance"] == "vllm-triage"
    assert vllm.requests[0].guided_json == Answer.model_json_schema()
    assert vllm.requests[0].max_tokens == 200


def test_generate_retries_once_with_the_error_as_a_tool_result_and_reports_tier_1(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(put_body([announce("vllm-triage")], roles={"triage": "vllm-triage"}))
    vllm.script("vllm-triage", "not json at all")
    vllm.script_json("vllm-triage", {"owner": "EE", "severity": "S1"})
    answer = harness.client.post("/v1/generate", json=generate_body()).json()
    assert answer["tier"] == 1 and answer["object"]["severity"] == "S1"
    retry = vllm.requests[1]
    assert retry.messages[-1].role == "tool"
    assert "the answer is not JSON" in retry.messages[-1].content


def test_generate_that_never_validates_is_a_422_in_three_parts(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(put_body([announce("vllm-triage")], roles={"triage": "vllm-triage"}))
    vllm.script_json("vllm-triage", {"owner": "EE", "severity": "S9"})
    vllm.script_json("vllm-triage", {"owner": "EE"})
    body = assert_problem(
        harness.client.post("/v1/generate", json=generate_body(max_retries=1)), 422
    )
    assert body["what_happened"] == "vllm-triage did not produce a valid answer in 2 attempts."
    assert body["likely_cause"].endswith("severity: is missing")
    # The breaker opened after two invalid answers; the next request says so.
    vllm.script_json("vllm-triage", {"owner": "EE", "severity": "S1"})
    paused = assert_problem(harness.client.post("/v1/generate", json=generate_body()), 503)
    assert paused["likely_cause"].startswith("vllm-triage is paused until")


# --- POST /v1/cross-check ---------------------------------------------------------------------


def test_cross_check_returns_the_verdict_with_one_vote_per_voter(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(put_body([announce(v) for v in VOTERS], roles={}, voters=VOTERS))
    for voter in VOTERS:
        vllm.script_json(voter, vote_json())
    response = harness.client.post(
        "/v1/cross-check",
        json={
            "decision": "plan_approval",
            "evidence": [{"role": "user", "content": "Plan: 25 DC cycles. password=hunter22"}],
        },
    )
    assert response.status_code == 200, response.text
    verdict = ConsensusVerdict.model_validate(response.json())
    assert verdict.agreed and verdict.sentence == "3 of 3 approve the plan."
    assert [vote.voter for vote in verdict.votes] == VOTERS
    for sent in vllm.requests:
        assert "hunter22" not in sent.messages[1].content
    status = harness.client.get("/v1/status").json()
    assert status["budget"]["limit_pct"] == 5.0 and status["budget"]["used_pct"] > 0


def test_cross_check_without_voters_is_degraded_and_unknown_decisions_are_400(
    harness: Harness,
) -> None:
    response = harness.client.post(
        "/v1/cross-check", json={"decision": "code_change", "evidence": USER}
    )
    verdict = response.json()
    assert response.status_code == 200 and verdict["degraded"] and not verdict["agreed"]
    body = assert_problem(
        harness.client.post("/v1/cross-check", json={"decision": "vibes", "evidence": USER}), 400
    )
    assert body["what_happened"] == "There is no cross-check rule called vibes."


# --- GET /v1/status ---------------------------------------------------------------------------


def test_status_says_what_the_models_page_needs(harness: Harness) -> None:
    before = harness.client.get("/v1/status").json()
    assert before["sentence"] == (
        "No model instance has reported yet; the model manager is still starting them."
    )
    assert before["budget"] == {"used_pct": 0.0, "limit_pct": 5.0}
    assert {row["role"] for row in before["roles"]} == {
        "coder",
        "planner",
        "triage",
        "embed",
        "rerank",
    }
    assert all(row["healthy"] is False and row["model_id"] is None for row in before["roles"])

    harness.put(
        put_body(
            [announce("vllm-coder", model_id="qwen3-coder"), announce(VOTERS[0])],
            roles={"coder": "vllm-coder", "triage": "vllm-triage"},
            voters=VOTERS[:2],
        )
    )
    after = harness.client.get("/v1/status").json()
    assert after["roles"] == [
        {"role": "coder", "instance": "vllm-coder", "model_id": "qwen3-coder", "healthy": True},
        {"role": "triage", "instance": "vllm-triage", "model_id": None, "healthy": False},
    ]
    assert after["sentence"] == (
        "1 of 2 roles have a healthy instance; 1 of 2 voters are ready. "
        "Cross-checks have used 0 of 50,000 tokens today (5% of the daily allowance)."
    )


# --- the instance table and the httpx client -------------------------------------------------


def test_instance_table_resolves_only_healthy_entries() -> None:
    table = InstanceTable(FakeClock(START, step=timedelta(seconds=0)))
    assert table.empty() and table.names() == [] and table.get("vllm-coder") is None
    with pytest.raises(InstanceUnavailableError) as raised:
        table.resolve("vllm-coder")
    assert str(raised.value) == not_registered("vllm-coder")
    newly = table.replace(
        [
            InstanceAnnouncement(name="vllm-coder", url="http://vllm-coder:8000/", model_id="m1"),
            InstanceAnnouncement(
                name="vllm-triage", url="http://vllm-triage:8000", model_id="m2", healthy=False
            ),
        ]
    )
    assert newly == ["vllm-coder"]
    resolved = table.resolve("vllm-coder")
    assert resolved.url == "http://vllm-coder:8000" and resolved.model == "m1"
    with pytest.raises(InstanceUnavailableError):
        table.resolve("vllm-triage")
    assert table.is_healthy("vllm-coder") and not table.is_healthy("vllm-triage")
    assert table.replace([]) == [] and table.empty()


class VllmDouble:
    """An OpenAI-compatible endpoint behind `httpx.MockTransport`; records what it was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Callable[[httpx.Request], httpx.Response] = self.ok

    def ok(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2},
            },
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def test_put_then_a_completion_reaches_the_announced_url_with_the_model_id(tmp_path: Path) -> None:
    double = VllmDouble()
    clock = FakeClock(START, step=timedelta(seconds=0))
    table = InstanceTable(clock)
    harness = Harness.__new__(Harness)
    app = create_app(
        settings=Settings(
            consensus_file=tmp_path / "none.yaml", redaction_file=tmp_path / "none.yaml"
        ),
        instances=table,
        clock=clock,
        log=EventLog("llm-gateway", ListSink()),
        client=HttpxVllmClient(table, transport=double.transport()),
    )
    harness.app, harness.client = app, TestClient(app)
    harness.put(
        put_body([announce("vllm-coder", model_id="qwen3-coder")], roles={"coder": "vllm-coder"})
    )
    response = harness.client.post(
        "/v1/complete",
        json={"role": "coder", "messages": USER},
        headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "hello" and response.json()["finish_reason"] == "length"
    assert response.json()["prompt_tokens"] == 11 and response.json()["completion_tokens"] == 2
    sent = double.requests[0]
    assert str(sent.url) == "http://vllm-coder:8000/v1/chat/completions"
    body = json.loads(sent.content)
    assert body["model"] == "qwen3-coder" and body["messages"] == USER
    assert "guided_json" not in body
    assert sent.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert sent.headers["X-Slas-Trace-Id"] == "4bf92f3577b34da6a3ce929d0e0e4736"


def test_httpx_client_turns_every_failure_into_instance_unavailable() -> None:
    double = VllmDouble()
    table = InstanceTable(FakeClock(START, step=timedelta(seconds=0)))
    client = HttpxVllmClient(table, transport=double.transport(), timeout_s=5)
    request = CompletionRequest(
        instance="vllm-coder",
        messages=[Message(role="user", content="hi")],
        guided_json={"type": "object"},
    )
    with pytest.raises(InstanceUnavailableError, match="has been announced"):
        client.complete(request)
    table.replace([InstanceAnnouncement(name="vllm-coder", url="http://c:8000", model_id="m")])

    def connect_error(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    double.answer = connect_error
    with pytest.raises(InstanceUnavailableError, match="did not answer"):
        client.complete(request)
    double.answer = lambda _request: httpx.Response(500, text="boom")
    with pytest.raises(InstanceUnavailableError, match="answered 500"):
        client.complete(request)
    double.answer = lambda _request: httpx.Response(200, text="not json")
    with pytest.raises(InstanceUnavailableError, match="did not answer"):
        client.complete(request)
    double.answer = lambda _request: httpx.Response(200, json={"choices": []})
    with pytest.raises(InstanceUnavailableError, match="shape the gateway does not understand"):
        client.complete(request)
    double.answer = lambda _request: httpx.Response(
        200, json={"choices": [{"message": {"content": "ok"}}]}
    )
    response = client.complete(request)
    assert response.text == "ok" and response.finish_reason == "stop"
    assert response.prompt_tokens > 0 and response.completion_tokens > 0, "estimated"
    assert json.loads(double.requests[-1].content)["guided_json"] == {"type": "object"}
    client.close()


# --- settings and the YAML loaders -----------------------------------------------------------


def test_settings_come_from_the_environment_with_defaults() -> None:
    defaults = Settings.from_env({})
    assert defaults == Settings()
    assert defaults.bind == "0.0.0.0:8000"
    assert defaults.consensus_file == Path("/etc/slas/consensus.yaml")
    assert defaults.redaction_file == Path("/etc/slas/redaction.yaml")
    assert defaults.token_budget_pct is None
    custom = Settings.from_env(
        {
            "SLAS_BIND": "127.0.0.1:9000",
            "SLAS_CONSENSUS_FILE": "/etc/slas/custom/c.yaml",
            "SLAS_REDACTION_FILE": "/etc/slas/custom/r.yaml",
            "SLAS_VLLM_TIMEOUT_S": "30",
            "SLAS_DAILY_TOKENS": "5000",
            "CONSENSUS_TOKEN_BUDGET_PCT": "2.5",
        }
    )
    assert custom.bind == "127.0.0.1:9000" and custom.vllm_timeout_s == 30.0
    assert custom.consensus_file == Path("/etc/slas/custom/c.yaml") and custom.daily_tokens == 5000
    assert custom.token_budget_pct == 2.5
    with pytest.raises(SettingsError) as raised:
        Settings.from_env({"SLAS_DAILY_TOKENS": "lots"})
    assert raised.value.message.what_happened == "The setting SLAS_DAILY_TOKENS could not be read."
    assert "whole number" in raised.value.message.likely_cause
    with pytest.raises(SettingsError) as zero:
        Settings.from_env({"CONSENSUS_TOKEN_BUDGET_PCT": "0"})
    assert zero.value.message.likely_cause == "It is 0; the gateway needs a value above zero."


def test_yaml_files_load_the_shipped_rules_and_missing_files_fall_back_with_a_warning(
    tmp_path: Path,
) -> None:
    sink = ListSink()
    log = EventLog("llm-gateway", sink)
    rules = load_consensus_rules(REPO_ROOT / "config" / "consensus.yaml", log)
    assert list(rules.decisions) == [
        "plan_approval",
        "code_change",
        "ticket_diagnosis",
        "factory_pass",
        "rca_conclusion",
    ]
    redactor = load_redactor(REPO_ROOT / "config" / "redaction.yaml", log)
    assert (
        redactor.redact("password=Sup3rSecret!").text == "password=[redacted:password_assignment]"
    )
    assert [r["event"] for r in sink.records()] == ["config.loaded", "config.loaded"]

    fallback_rules = load_consensus_rules(tmp_path / "no-such.yaml", log)
    fallback_redactor = load_redactor(tmp_path / "no-such.yaml", log)
    assert fallback_rules.model_dump() == rules.model_dump()
    assert fallback_redactor.rules.model_dump() == redactor.rules.model_dump()
    warnings = [r for r in sink.records() if r["event"] == "config.default_used"]
    assert [w["what"] for w in warnings] == ["consensus rules", "redaction rules"]
    assert all(w["level"] == "warning" for w in warnings)


def test_a_malformed_yaml_file_stops_the_start_in_three_parts(tmp_path: Path) -> None:
    log = EventLog("llm-gateway", ListSink())
    broken = tmp_path / "consensus.yaml"
    broken.write_text("version: [1\n", encoding="utf-8")
    with pytest.raises(SettingsError) as raised:
        load_consensus_rules(broken, log)
    assert (
        raised.value.message.what_happened
        == f"The cross-check rules in {broken} could not be read."
    )
    assert raised.value.message.likely_cause.startswith("The file is not valid YAML")

    wrong = tmp_path / "redaction.yaml"
    wrong.write_text("version: 1\nrules: []\n", encoding="utf-8")
    with pytest.raises(SettingsError) as invalid:
        load_redactor(wrong, log)
    assert (
        invalid.value.message.what_happened == f"The redaction rules in {wrong} could not be used."
    )

    not_rules = tmp_path / "c2.yaml"
    not_rules.write_text("version: 1\ndecisions: {}\nextra: true\n", encoding="utf-8")
    with pytest.raises(SettingsError) as extra:
        load_consensus_rules(not_rules, log)
    assert "extra" in extra.value.message.likely_cause


def test_create_app_uses_the_rules_files_and_the_budget_override(tmp_path: Path) -> None:
    consensus = tmp_path / "consensus.yaml"
    consensus.write_text(
        (REPO_ROOT / "config" / "consensus.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    app = create_app(
        settings=Settings(
            consensus_file=consensus,
            redaction_file=tmp_path / "missing.yaml",
            daily_tokens=100_000,
            token_budget_pct=2.0,
        ),
        log=EventLog("llm-gateway", ListSink()),
    )
    gateway: Gateway = app.state.gateway
    assert gateway.budget.pct == 2.0 and gateway.budget.cap == 2_000
    assert isinstance(gateway.client, HttpxVllmClient)
    status = TestClient(app).get("/v1/status").json()
    assert status["budget"] == {"used_pct": 0.0, "limit_pct": 2.0}


# --- the CLI ---------------------------------------------------------------------------------


def test_slas_gateway_serve_builds_the_app_and_serves_on_the_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SLAS_BIND", "127.0.0.1:8123")
    monkeypatch.setenv("SLAS_CONSENSUS_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("SLAS_REDACTION_FILE", str(tmp_path / "none.yaml"))
    served: list[tuple[FastAPI, str]] = []

    def fake_run(app: FastAPI, bind: str) -> None:
        served.append((app, bind))

    assert cli.main(["serve"], run=fake_run) == 0
    assert len(served) == 1 and served[0][1] == "127.0.0.1:8123"
    assert ("PUT", "/v1/instances") in route_table(served[0][0])


def test_slas_gateway_serve_stops_with_three_parts_on_a_bad_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLAS_DAILY_TOKENS", "many")
    err = io.StringIO()
    assert cli.main(["serve"], err=err, run=lambda _app, _bind: None) == 1
    lines = err.getvalue().splitlines()
    assert lines[0] == "The setting SLAS_DAILY_TOKENS could not be read."
    assert len(lines) == 3


def test_the_cli_refuses_an_unknown_command() -> None:
    with pytest.raises(SystemExit):
        cli.main(["dance"], err=io.StringIO(), run=lambda _app, _bind: None)


# --- HttpGateway -----------------------------------------------------------------------------


def via_test_client(test_client: TestClient) -> httpx.MockTransport:
    """An httpx transport that hands each request to the in-process app."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = test_client.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        )
        return httpx.Response(
            answer.status_code,
            content=answer.content,
            headers={"content-type": answer.headers.get("content-type", "application/json")},
        )

    return httpx.MockTransport(handler)


def test_http_gateway_round_trips_every_method_with_the_gateway_types(
    harness: Harness, vllm: FakeVllm
) -> None:
    harness.put(
        put_body(
            [announce("vllm-coder"), announce("vllm-triage"), *(announce(v) for v in VOTERS)],
            roles={"coder": "vllm-coder", "triage": "vllm-triage"},
            voters=VOTERS,
        )
    )
    vllm.script("vllm-coder", "print('hi')")
    vllm.script_json("vllm-triage", {"owner": "EE", "severity": "S2"})
    vllm.script_json("vllm-triage", {"owner": "ME", "severity": "S3"})
    for voter in VOTERS:
        vllm.script_json(voter, vote_json("concern", "missing a test"))
    remote = HttpGateway("http://llm-gateway:8000", transport=via_test_client(harness.client))
    like: GatewayLike = remote
    also: GatewayLike = harness.gateway
    assert like is remote and also is harness.gateway

    completion = remote.complete(
        "coder", [Message(role="user", content="hi")], max_tokens=32, temperature=0.1
    )
    assert completion.text == "print('hi')" and completion.instance == "vllm-coder"
    assert vllm.requests[-1].max_tokens == 32 and vllm.requests[-1].temperature == 0.1

    structured = remote.generate("triage", [Message(role="user", content="judge")], Answer)
    assert structured.value == Answer(owner="EE", severity="S2")
    assert structured.instance == "vllm-triage" and structured.attempts == 1
    assert structured.tier == 0 and structured.tokens == structured.response.total_tokens
    assert vllm.requests[-1].guided_json == Answer.model_json_schema()

    loose = remote.generate_json(
        "triage", [Message(role="user", content="judge")], Answer.model_json_schema()
    )
    assert loose.value == {"owner": "ME", "severity": "S3"}

    verdict = remote.cross_check("code_change", [Message(role="user", content="diff")])
    assert isinstance(verdict, ConsensusVerdict)
    assert not verdict.agreed and len(verdict.concerns) == 3
    remote.close()


def test_http_gateway_passes_the_service_s_three_parts_on(harness: Harness) -> None:
    remote = HttpGateway("http://llm-gateway:8000", transport=via_test_client(harness.client))
    with pytest.raises(ServiceError) as raised:
        remote.complete("coder", [Message(role="user", content="hi")])
    assert raised.value.status == 503
    assert raised.value.message.what_happened == "No instance serves the role coder yet."
    with pytest.raises(ServiceError) as unknown:
        remote.cross_check("vibes", [Message(role="user", content="x")])
    assert unknown.value.status == 400


# --- schema_check ---------------------------------------------------------------------------


class Edit(SlasModel):
    path: str = Field(min_length=1)
    lines: list[int] = Field(min_length=1, max_length=3)


class EditSet(SlasModel):
    title: str
    edits: list[Edit]
    note: str | None = None
    kind: str = Field(default="patch", pattern=r"^(patch|rewrite)$")


def test_schema_check_accepts_what_pydantic_accepts_and_names_the_first_problem() -> None:
    schema = EditSet.model_json_schema()
    good = {"title": "t", "edits": [{"path": "a.py", "lines": [1, 2]}], "note": None}
    check(good, schema)
    assert validate_json(json.dumps(good), schema) == good
    cases: list[tuple[dict[str, Any], str]] = [
        ({"edits": []}, "title: is missing"),
        (
            {"title": "t", "edits": [{"path": "", "lines": [1]}]},
            "edits.0.path: must have at least 1 character",
        ),
        (
            {"title": "t", "edits": [{"path": "p", "lines": []}]},
            "edits.0.lines: must have at least 1 items",
        ),
        (
            {"title": "t", "edits": [{"path": "p", "lines": [1, 2, 3, 4]}]},
            "edits.0.lines: must have at most 3 items",
        ),
        (
            {"title": "t", "edits": [{"path": "p", "lines": ["x"]}]},
            "edits.0.lines.0: must be integer, not a string",
        ),
        (
            {"title": "t", "edits": [], "kind": "delete"},
            "kind: does not match the pattern ^(patch|rewrite)$",
        ),
        ({"title": "t", "edits": [], "extra": 1}, "extra: is not an allowed field"),
        ({"title": 5, "edits": []}, "title: must be string, not an integer"),
        (
            {"title": "t", "edits": [], "note": 3},
            "note: matches none of the allowed forms "
            "(must be string, not an integer; must be null, not an integer)",
        ),
    ]
    for value, sentence in cases:
        with pytest.raises(SchemaMismatchError) as raised:
            check(value, schema)
        assert str(raised.value) == sentence, value
    with pytest.raises(SchemaMismatchError, match="the answer is not JSON"):
        validate_json("{not json", schema)


def test_schema_check_covers_enum_const_bounds_alternatives_and_references() -> None:
    check(3, {"type": "integer", "minimum": 1, "maximum": 3})
    check(3.0, {"type": "integer"})
    check(True, {"type": "boolean"})
    check(None, {"type": ["string", "null"]})
    check([1, "a"], {"type": "array", "prefixItems": [{"type": "integer"}, {"type": "string"}]})
    check({"x": 1}, {"type": "object", "additionalProperties": {"type": "integer"}})
    check(2, {"allOf": [{"type": "integer"}, {"minimum": 2}]})
    check("b", {"enum": ["a", "b"]})
    check("only", {"const": "only"})
    check(1, {"oneOf": [{"type": "integer"}, {"type": "string"}]})
    failures: list[tuple[object, dict[str, Any], str]] = [
        (True, {"type": "integer"}, "must be integer, not a boolean"),
        (0, {"type": "integer", "minimum": 1}, "must be at least 1"),
        (4, {"maximum": 3}, "must be at most 3"),
        (1, {"exclusiveMinimum": 1}, "must be greater than 1"),
        (3, {"exclusiveMaximum": 3}, "must be less than 3"),
        ("abcd", {"maxLength": 3}, "must have at most 3 characters"),
        ("c", {"enum": ["a", "b"]}, 'must be one of "a", "b"'),
        ("other", {"const": "only"}, 'must be "only"'),
        (
            1,
            {"oneOf": [{"type": "integer"}, {"type": "number"}]},
            "matches more than one of the allowed forms",
        ),
        (
            {"x": "s"},
            {"type": "object", "additionalProperties": {"type": "integer"}},
            "x: must be integer, not a string",
        ),
        (1, {"allOf": [{"type": "integer"}, {"minimum": 2}]}, "must be at least 2"),
        ([1], {"type": "array", "items": {"type": "string"}}, "0: must be string, not an integer"),
        (1, {"type": "thing"}, "the schema names an unknown type 'thing'"),
        (
            1,
            {"$ref": "https://elsewhere/schema"},
            "the schema uses a reference this checker cannot follow: https://elsewhere/schema",
        ),
        (
            1,
            {"$ref": "#/$defs/missing"},
            "the schema references #/$defs/missing, which does not exist",
        ),
        (
            1,
            {"$defs": {"n": 5}, "$ref": "#/$defs/n"},
            "the schema references #/$defs/n, which is not a schema",
        ),
        (
            1,
            {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
            "the schema nests deeper than the checker follows",
        ),
    ]
    for value, schema, sentence in failures:
        with pytest.raises(SchemaMismatchError) as raised:
            check(value, schema)
        assert str(raised.value) == sentence, (value, schema)
    # A `$ref` with an escaped pointer segment resolves.
    check(1, {"$defs": {"a/b": {"type": "integer"}}, "$ref": "#/$defs/a~1b"})
