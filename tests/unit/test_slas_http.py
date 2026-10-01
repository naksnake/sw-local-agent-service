"""slas_http: the shared service app, three-part errors, identity headers, the client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from slas_http.app import create_service_app
from slas_http.client import ServiceClient, ServiceUnreachableError
from slas_http.errors import ServiceError
from slas_http.identity import (
    CAPABILITIES_HEADER,
    USER_HEADER,
    Identity,
    identity_of,
    require,
    require_any,
)
from slas_http.serve import split_bind
from slas_observability import tracing
from slas_observability.events import EventLog, ListSink
from slas_schemas.errors import ThreePartMessage

THREE_PARTS = ("what_happened", "likely_cause", "what_to_do")


def make_app(*, checks: dict[str, str] | None = None) -> tuple[TestClient, ListSink]:
    router = APIRouter(prefix="/v1")

    @router.get("/who")
    def who(request: Request) -> dict[str, Any]:
        identity = identity_of(request)
        require(identity, "git:push_branch", verb="push a branch")
        return {"user": identity.user, "name": identity.name}

    @router.get("/boom")
    def boom() -> dict[str, Any]:
        raise RuntimeError("nobody caught this")

    @router.get("/refuse")
    def refuse() -> dict[str, Any]:
        raise ServiceError.build(409, "The lease is taken.", "Another run holds it.", "Wait.")

    @router.post("/echo")
    def echo(body: dict[str, Any]) -> dict[str, Any]:
        return body

    sink = ListSink()
    app = create_service_app(
        "unit-service",
        checks=(lambda: checks) if checks is not None else None,
        log=EventLog("unit-service", sink),
        routers=(router,),
    )
    return TestClient(app, raise_server_exceptions=False), sink


def test_health_metrics_and_no_docs_page() -> None:
    client, _ = make_app(checks={"runtime": "ok"})
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"service": "unit-service", "ok": True, "checks": {"runtime": "ok"}}
    assert client.get("/metrics").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_a_declared_healthy_state_is_not_a_failure() -> None:
    sink = ListSink()
    app = create_service_app(
        "unit-service",
        checks=lambda: {"runtime": "ok", "isolation": "runc"},
        log=EventLog("unit-service", sink),
        healthy_states=frozenset({"ok", "runc", "gvisor"}),
    )
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["checks"] == {"runtime": "ok", "isolation": "runc"}


def test_a_failing_check_is_a_three_part_503_naming_it() -> None:
    client, _ = make_app(checks={"runtime": "down", "gateway": "ok"})
    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["what_happened"] == "The unit-service is not healthy: runtime did not answer."
    assert "slas logs runtime" in body["what_to_do"]


def test_every_error_shape_is_three_parts_with_a_trace_id() -> None:
    client, sink = make_app()
    for path, status in (("/v1/refuse", 409), ("/nowhere", 404), ("/v1/boom", 500)):
        response = client.get(path, headers={tracing.SLAS_HEADER: "ab" * 16})
        assert response.status_code == status, path
        body = response.json()
        assert set(body) == {*THREE_PARTS, "trace_id"}
        assert body["trace_id"] == "ab" * 16
        assert response.headers[tracing.SLAS_HEADER] == "ab" * 16
    assert client.delete("/v1/echo").status_code == 405
    bad = client.post("/v1/echo", content=b"not json", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400
    assert bad.json()["what_happened"].startswith("The request couldn't be read")
    unexpected = [r for r in sink.records() if r["event"] == "request.unexpected_error"]
    assert unexpected and unexpected[0]["error_type"] == "RuntimeError"


def test_identity_headers_and_authz_where_the_action_executes() -> None:
    client, _ = make_app()
    missing = client.get("/v1/who")
    assert missing.status_code == 401
    assert missing.json()["what_happened"] == "The request did not say who is acting."
    lacking = Identity("pat@slas.local", "Pat", frozenset({"git:pull"}))
    refused = client.get("/v1/who", headers=lacking.headers())
    assert refused.status_code == 403
    assert refused.json()["what_happened"] == "Pat may not push a branch."
    allowed = Identity("pat@slas.local", "", frozenset({"git:pull", "git:push_branch"}))
    ok = client.get("/v1/who", headers=allowed.headers())
    assert ok.json() == {"user": "pat@slas.local", "name": "pat@slas.local"}
    assert allowed.headers()[CAPABILITIES_HEADER] == "git:pull,git:push_branch"
    parsed = Identity.from_headers(
        {USER_HEADER.lower(): " pat@slas.local ", "x-slas-capabilities": "a, ,b"}
    )
    assert parsed is not None and parsed.capabilities == frozenset({"a", "b"})
    assert Identity.from_headers({}) is None
    assert Identity.system().user == "system@slas.local"
    with pytest.raises(ServiceError) as raised:
        require_any(lacking, ["screen", "ssh"], verb="operate the station")
    assert raised.value.status == 403
    assert raised.value.message.what_happened == "Pat may not operate the station."
    require_any(lacking, ["git:pull"], verb="pull")


def _mock_client(handler: Any, **kwargs: Any) -> ServiceClient:
    return ServiceClient(
        "unit-service", "http://unit", transport=httpx.MockTransport(handler), **kwargs
    )


def test_client_forwards_trace_and_identity_and_decodes_json() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"answer": 42})

    client = _mock_client(handler)
    identity = Identity("pat@slas.local", "Pat", frozenset({"git:pull"}))
    with tracing.trace("cd" * 16):
        assert client.post("/v1/echo", {"a": 1}, identity=identity) == {"answer": 42}
    assert seen["headers"][tracing.SLAS_HEADER.lower()] == "cd" * 16
    assert seen["headers"]["traceparent"].startswith("00-" + "cd" * 16)
    assert seen["headers"]["x-slas-user"] == "pat@slas.local"
    assert seen["headers"]["x-slas-display-name"] == "Pat"
    assert seen["body"] == b'{"a":1}'
    assert client.get("/v1/x", params={"q": "1"}) == {"answer": 42}
    assert seen["url"] == "http://unit/v1/x?q=1"
    assert client.put("/v1/x", {"b": 2}) == {"answer": 42}
    assert client.delete("/v1/x") is None
    client.close()


def test_client_turns_three_part_bodies_and_failures_into_service_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/three":
            return httpx.Response(
                409,
                json={
                    "what_happened": "Taken.",
                    "likely_cause": "Busy.",
                    "what_to_do": "Wait.",
                    "trace_id": "x",
                },
            )
        if request.url.path == "/bare":
            return httpx.Response(502, text="Bad Gateway")
        if request.url.path == "/notjson":
            return httpx.Response(200, text="<html>")
        if request.url.path == "/timeout":
            raise httpx.ReadTimeout("slow")
        raise httpx.ConnectError("refused")

    client = _mock_client(handler)
    with pytest.raises(ServiceError) as three:
        client.get("/three")
    assert three.value.status == 409
    assert three.value.message == ThreePartMessage("Taken.", "Busy.", "Wait.")
    with pytest.raises(ServiceError) as bare:
        client.get("/bare")
    assert bare.value.status == 502
    assert "without a sentence of its own" in bare.value.message.likely_cause
    with pytest.raises(ServiceUnreachableError) as notjson:
        client.get("/notjson")
    assert "without JSON" in notjson.value.message.likely_cause
    with pytest.raises(ServiceUnreachableError) as timeout:
        client.get("/timeout", timeout_s=1.0)
    assert timeout.value.status == 503
    assert "timed out" in timeout.value.message.likely_cause
    with pytest.raises(ServiceUnreachableError) as down:
        client.get("/down")
    assert down.value.message.what_happened == "The unit-service did not answer."
    assert "slas logs unit-service" in down.value.message.what_to_do


def test_split_bind() -> None:
    every_interface = "0.0.0.0"  # noqa: S104 — the container's own interface
    assert split_bind(f"{every_interface}:8000") == (every_interface, 8000)
    assert split_bind(":9000") == (every_interface, 9000)
    assert split_bind("127.0.0.1:81") == ("127.0.0.1", 81)
