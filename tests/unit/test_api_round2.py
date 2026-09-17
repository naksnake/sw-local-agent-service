"""The round-2 browser routes: proxying with authz (docs/api-contract-round-2.md §8).

Every downstream is an `httpx.MockTransport` that records what the api sent; nothing here
reaches a network, a sandbox or a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from slas_api import proxy, routes_round2
from slas_api.app import route_table
from slas_api.routes_round2 import ROUTES, ProxyRoute, browser_routes, first_session_id
from slas_api.service import Downstream
from slas_api.settings import Settings
from slas_authz import Capability, Principal
from tests.unit.api_harness import (
    ADMIN_EMAIL,
    DOWNSTREAM_SERVICES,
    INITIAL_PASSWORD,
    REPO_ROOT,
    WEBUI,
    Harness,
    fake_downstream,
    make_harness,
)
from tests.unit.test_api_session import assert_problem

THREE_PART_409 = {
    "what_happened": "Someone else holds this remote.",
    "likely_cause": "A push from another session is still running.",
    "what_to_do": "Wait for it to finish and try again.",
    "trace_id": "0" * 32,
}

PARAM_VALUES = {"id": "T-1", "ticket_id": "T-coding-7", "slug": "demo-proj", "name": "st-01"}


@dataclass
class Seen:
    service: str
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass
class Recorder:
    """Records every downstream call and answers what `answers[(service, path)]` says."""

    calls: list[Seen] = field(default_factory=list)
    answers: dict[tuple[str, str], httpx.Response] = field(default_factory=dict)
    unreachable: set[str] = field(default_factory=set)

    def handler_for(self, service: str) -> Any:
        def handle(request: httpx.Request) -> httpx.Response:
            if service in self.unreachable:
                raise httpx.ConnectError("connection refused", request=request)
            body = json.loads(request.content) if request.content else None
            self.calls.append(
                Seen(
                    service,
                    request.method,
                    request.url.path,
                    dict(request.url.params),
                    dict(request.headers),
                    body,
                )
            )
            answer = self.answers.get((service, request.url.path))
            if answer is not None:
                return answer
            return httpx.Response(200, json={"service": service, "path": request.url.path})

        return handle

    def downstream(self) -> Downstream:
        return fake_downstream({name: self.handler_for(name) for name in DOWNSTREAM_SERVICES})

    def paths(self, service: str) -> list[tuple[str, str]]:
        return [(c.method, c.path) for c in self.calls if c.service == service]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def harness(tmp_path: Path, recorder: Recorder) -> Harness:
    return make_harness(tmp_path, downstream=recorder.downstream())


@pytest.fixture
def admin(harness: Harness) -> TestClient:
    return harness.sign_in_admin()


def person_with_role(harness: Harness, admin: TestClient, email: str, role: str) -> TestClient:
    _, one_time = harness.add_person(email, email.split("@")[0].capitalize(), role, client=admin)
    return harness.client_for(email, one_time, f"{role}-own-long-password")


def browser_path(route: ProxyRoute) -> str:
    return route.browser_path.format(**PARAM_VALUES)


def downstream_path(route: ProxyRoute) -> str:
    return route.downstream_path.format(**PARAM_VALUES)


# --- forwarding ------------------------------------------------------------------------------


def test_identity_headers_body_and_query_are_forwarded(
    harness: Harness, admin: TestClient, recorder: Recorder
) -> None:
    me = admin.get("/api/v1/me").json()
    body = {"breakdown": {"title": "Fix the parser"}, "plan": "# plan\n", "filename": "plan.md"}
    response = admin.post("/api/v1/coding/tasks", json=body, headers=WEBUI)
    assert response.status_code == 200, response.text
    assert response.json() == {"service": "orchestrator", "path": "/v1/coding/tasks"}
    (call,) = recorder.calls
    assert (call.service, call.method, call.path) == ("orchestrator", "POST", "/v1/coding/tasks")
    assert call.body == body, "the body passes through unchanged"
    assert call.headers["x-slas-user"] == ADMIN_EMAIL
    assert call.headers["x-slas-display-name"] == "Administrator"
    assert call.headers["x-slas-capabilities"] == ",".join(sorted(me["capabilities"]))
    assert "model:manage" in call.headers["x-slas-capabilities"]
    assert "cookie" not in call.headers and "authorization" not in call.headers
    assert call.headers["x-slas-trace-id"] == response.headers["X-Slas-Trace-Id"]

    listed = admin.get("/api/v1/tickets", params={"state": "Open", "limit": "5"})
    assert listed.status_code == 200
    call = recorder.calls[-1]
    assert (call.method, call.path) == ("GET", "/v1/tickets")
    assert call.query == {"state": "Open", "limit": "5"}
    assert call.body is None, "a GET carries no body downstream"


def test_every_route_in_the_table_reaches_its_service_at_its_path(
    admin: TestClient, recorder: Recorder
) -> None:
    for route in ROUTES:
        recorder.calls.clear()
        payload = None if route.method in {"GET", "DELETE"} else {"probe": route.path}
        response = admin.request(route.method, browser_path(route), json=payload, headers=WEBUI)
        assert response.status_code == 200, (route, response.text)
        assert len(recorder.calls) == 1, route
        call = recorder.calls[0]
        assert call.service == route.service, route
        assert call.method == route.method, route
        assert call.path == downstream_path(route), route
        assert call.body == payload, route
        assert call.headers["x-slas-user"] == ADMIN_EMAIL
        assert response.json()["path"] == downstream_path(route)


def test_path_parameters_are_url_safe_downstream(admin: TestClient, recorder: Recorder) -> None:
    response = admin.get("/api/v1/tickets/T-validation-3%20draft")
    assert response.status_code == 200
    assert recorder.calls[0].path == "/v1/tickets/T-validation-3 draft"
    assert routes_round2.fill_path("/v1/x/{id}", {"id": "a/b c"}) == "/v1/x/a%2Fb%20c"


def test_a_downstream_three_part_error_keeps_its_status_and_sentences(
    admin: TestClient, recorder: Recorder
) -> None:
    recorder.answers[("git_broker", "/v1/projects/demo-proj/push")] = httpx.Response(
        409, json=THREE_PART_409
    )
    response = admin.post(
        "/api/v1/git/projects/demo-proj/push",
        json={"remote_id": "r1", "branch": "feature"},
        headers=WEBUI,
    )
    body = assert_problem(response, 409)
    for key in ("what_happened", "likely_cause", "what_to_do"):
        assert body[key] == THREE_PART_409[key]
    assert body["trace_id"] != THREE_PART_409["trace_id"], "the api's own trace id"

    recorder.answers[("orchestrator", "/v1/skills")] = httpx.Response(502, text="Bad Gateway")
    bare = assert_problem(admin.get("/api/v1/skills"), 502)
    assert bare["what_happened"] == "The agent-core-orchestrator refused the request."
    assert "slas logs agent-core-orchestrator" in bare["what_to_do"]


def test_an_unreachable_downstream_is_a_503_naming_the_service(
    admin: TestClient, recorder: Recorder
) -> None:
    recorder.unreachable.add("orchestrator")
    body = assert_problem(admin.get("/api/v1/coding/tasks"), 503)
    assert body["what_happened"] == "The agent-core-orchestrator did not answer."
    assert "slas logs agent-core-orchestrator" in body["what_to_do"]
    assert recorder.calls == [], "nothing was recorded because nothing answered"

    recorder.unreachable.add("model_manager")
    body = assert_problem(admin.get("/api/v1/models/status"), 503)
    assert "slas logs model-manager" in body["what_to_do"]


def test_a_downstream_without_a_body_is_an_empty_204(admin: TestClient, recorder: Recorder) -> None:
    recorder.answers[("git_broker", "/v1/remotes/T-1")] = httpx.Response(204)
    response = admin.delete("/api/v1/git/remotes/T-1", headers=WEBUI)
    assert response.status_code == 204 and response.content == b""


def test_a_body_that_is_not_json_is_a_400_before_any_call(
    admin: TestClient, recorder: Recorder
) -> None:
    response = admin.post(
        "/api/v1/coding/propose",
        content=b"plan: not json",
        headers={**WEBUI, "Content-Type": "application/json"},
    )
    body = assert_problem(response, 400)
    assert body["what_happened"] == "The request body is not JSON."
    assert recorder.calls == []


# --- authz before forwarding -----------------------------------------------------------------


def test_capabilities_are_required_before_any_downstream_call(
    harness: Harness, admin: TestClient, recorder: Recorder
) -> None:
    viewer = person_with_role(harness, admin, "vi@lab.local", "viewer")
    engineer = person_with_role(harness, admin, "en@lab.local", "engineer")
    lead = person_with_role(harness, admin, "le@lab.local", "line_lead")
    recorder.calls.clear()

    refused = assert_problem(
        viewer.post("/api/v1/validation/runs/T-1/approve", json={}, headers=WEBUI), 403
    )
    assert refused["what_happened"] == (
        "Vi may not approve destructive steps such as an AC cycle or a firmware flash."
    )
    assert refused["likely_cause"] == "The Viewer role does not include approve:destructive."
    assert recorder.calls == [], "refused before the orchestrator heard of it"

    cases = [
        (engineer, "POST", "/api/v1/factory/jobs/T-1/decide", "factory:verdict"),
        (viewer, "POST", "/api/v1/factory/jobs/T-1/control", "factory:control"),
        (engineer, "POST", "/api/v1/stations", "factory:stations_manage"),
        (engineer, "PUT", "/api/v1/stations/st-01/tuning", "factory:stations_manage"),
        (engineer, "DELETE", "/api/v1/stations/st-01", "factory:stations_manage"),
        (viewer, "POST", "/api/v1/git/remotes", "git:remote_manage"),
        (viewer, "DELETE", "/api/v1/git/remotes/r1", "git:remote_manage"),
        (engineer, "POST", "/api/v1/git/hosts", "git:hosts_manage"),
        (viewer, "POST", "/api/v1/git/projects/demo-proj/push", "git:push_branch"),
        (viewer, "POST", "/api/v1/git/projects/demo-proj/pull", "git:pull"),
        (viewer, "POST", "/api/v1/git/projects/demo-proj/bundle/export", "git:bundle"),
        (viewer, "POST", "/api/v1/git/projects/demo-proj/bundle/import", "git:bundle"),
        (viewer, "POST", "/api/v1/git/projects/demo-proj/terminal", "git:terminal"),
        (engineer, "POST", "/api/v1/models/swap", "model:manage"),
        (engineer, "POST", "/api/v1/models/rollback", "model:manage"),
    ]
    for client, method, path, capability in cases:
        body = assert_problem(client.request(method, path, json={}, headers=WEBUI), 403)
        assert body["likely_cause"].endswith(f"does not include {capability}."), (path, body)
    assert recorder.calls == []

    # "any of": the remote test needs git:clone or git:pull
    none = assert_problem(viewer.post("/api/v1/git/remotes/r1/test", json={}, headers=WEBUI), 403)
    assert none["what_happened"] == (
        "Vi may not clone from a saved Git remote or pull from a saved Git remote."
    )
    assert none["likely_cause"] == "The Viewer role includes none of git:clone, git:pull."
    assert recorder.calls == []
    assert engineer.post("/api/v1/git/remotes/r1/test", json={}, headers=WEBUI).status_code == 200
    assert recorder.paths("git_broker") == [("POST", "/v1/remotes/r1/test")]

    # the right role passes and the downstream sees that person
    recorder.calls.clear()
    decided = lead.post(
        "/api/v1/factory/jobs/T-1/decide", json={"verdict": "FAIL", "note": "x"}, headers=WEBUI
    )
    assert decided.status_code == 200
    assert recorder.calls[0].headers["x-slas-user"] == "le@lab.local"
    assert "factory:verdict" in recorder.calls[0].headers["x-slas-capabilities"]

    # reads need only a sign-in, whatever the role
    assert viewer.get("/api/v1/tickets").status_code == 200
    assert viewer.get("/api/v1/git/remotes").status_code == 200
    assert viewer.get("/api/v1/stations").status_code == 200
    assert viewer.get("/api/v1/models/status").status_code == 200


def test_require_any_with_no_capabilities_is_a_no_op() -> None:
    nobody = Principal("x@slas.local", "X", "viewer", "Viewer", frozenset())
    proxy.require_any(nobody, ())
    with pytest.raises(Exception, match="may not"):
        proxy.require_any(nobody, (Capability.GIT_PULL,))


def test_the_must_change_password_gate_still_applies(harness: Harness, recorder: Recorder) -> None:
    assert harness.sign_in(ADMIN_EMAIL, INITIAL_PASSWORD).status_code == 200
    gated = assert_problem(harness.client.get("/api/v1/coding/tasks"), 403)
    assert gated["what_happened"] == "Choose a new password first."
    assert_problem(harness.client.post("/api/v1/skills/import", json={}, headers=WEBUI), 403)
    assert recorder.calls == []


def test_without_a_session_every_route_is_401(harness: Harness, recorder: Recorder) -> None:
    for method, path, _ in browser_routes():
        response = harness.client.request(
            method, path.format(**PARAM_VALUES), json={}, headers=WEBUI
        )
        assert_problem(response, 401, reason="none")
    assert recorder.calls == []


def test_state_changing_requests_still_need_x_requested_with(
    admin: TestClient, recorder: Recorder
) -> None:
    for method, path in (
        ("POST", "/api/v1/coding/tasks"),
        ("PUT", "/api/v1/stations/st-01/tuning"),
        ("DELETE", "/api/v1/git/remotes/r1"),
        ("POST", "/api/v1/git/projects/demo-proj/terminal"),
    ):
        body = assert_problem(admin.request(method, path, json={}), 403)
        assert body["what_happened"] == (
            "The request didn't come from the SW Local Agent Service page."
        )
    assert recorder.calls == []
    assert admin.get("/api/v1/coding/tasks").status_code == 200, "GET needs no header"


# --- the terminal ----------------------------------------------------------------------------


def test_terminal_looks_the_session_up_then_sends_the_line(
    admin: TestClient, recorder: Recorder
) -> None:
    recorder.answers[("sandbox_manager", "/v1/sessions")] = httpx.Response(
        200, json=[{"id": "sess-42", "user": ADMIN_EMAIL, "slug": "demo-proj"}]
    )
    recorder.answers[("sandbox_manager", "/v1/sessions/sess-42/terminal")] = httpx.Response(
        200, json={"line": "git status", "output": "On branch main", "sentence": "Ran git status."}
    )
    response = admin.post(
        "/api/v1/git/projects/demo-proj/terminal", json={"line": "git status"}, headers=WEBUI
    )
    assert response.status_code == 200, response.text
    assert response.json()["output"] == "On branch main"
    lookup, line = recorder.calls
    assert (lookup.method, lookup.path) == ("GET", "/v1/sessions")
    assert lookup.query == {"user": ADMIN_EMAIL, "slug": "demo-proj"}
    assert lookup.headers["x-slas-user"] == ADMIN_EMAIL
    assert (line.method, line.path) == ("POST", "/v1/sessions/sess-42/terminal")
    assert line.body == {"line": "git status"}
    assert "git:terminal" in line.headers["x-slas-capabilities"]


def test_terminal_without_an_open_sandbox_is_a_409(admin: TestClient, recorder: Recorder) -> None:
    recorder.answers[("sandbox_manager", "/v1/sessions")] = httpx.Response(200, json=[])
    response = admin.post(
        "/api/v1/git/projects/demo-proj/terminal", json={"line": "ls"}, headers=WEBUI
    )
    body = assert_problem(response, 409)
    assert body["what_happened"] == "No sandbox is open for demo-proj."
    assert body["what_to_do"] == "Start a coding task or open the project first."
    assert recorder.paths("sandbox_manager") == [("GET", "/v1/sessions")], "no line was sent"

    recorder.unreachable.add("sandbox_manager")
    down = assert_problem(
        admin.post("/api/v1/git/projects/demo-proj/terminal", json={"line": "ls"}, headers=WEBUI),
        503,
    )
    assert "slas logs sandbox-manager" in down["what_to_do"]


def test_first_session_id_accepts_a_list_or_a_wrapped_list() -> None:
    assert first_session_id([{"id": "a"}, {"id": "b"}]) == "a"
    assert first_session_id({"sessions": [{"id": ""}, {"id": "b"}]}) == "b"
    assert first_session_id({"sessions": []}) is None
    assert first_session_id([]) is None
    assert first_session_id({"id": "not-a-list"}) is None
    assert first_session_id("nonsense") is None
    assert first_session_id([{"no_id": 1}, 3]) is None


# --- the table, the docs and the settings ------------------------------------------------------


def _documented_round_two() -> dict[tuple[str, str], str]:
    contract = (REPO_ROOT / "docs" / "api-contract.md").read_text(encoding="utf-8")
    section = contract.split("## Round 2", 1)[1]
    rows = re.findall(
        r"^\| `(GET|POST|PUT|DELETE) (/api/v1/[^\s`]+)` \| [^|]* \| ([^|]*) \|$",
        section,
        flags=re.MULTILINE,
    )
    return {(method, path): cell.strip() for method, path, cell in rows}


def test_the_browser_table_is_documented_with_its_capabilities() -> None:
    documented = _documented_round_two()
    table = browser_routes()
    assert {(m, p) for m, p, _ in table} == set(documented)
    for method, path, requires in table:
        cell = documented[(method, path)]
        if not requires:
            assert cell == "signed in", (method, path, cell)
        else:
            assert cell == " or ".join(f"`{c.value}`" for c in requires), (method, path, cell)
    assert all(pair in route_table() for pair in documented), "the app serves every row"
    assert len(table) == len(ROUTES) + 1 and table == sorted(table)
    methods = {m for m, _, _ in table}
    assert methods == {"GET", "POST", "PUT", "DELETE"}
    assert ("GET", "/api/v1/coding/tasks", ()) in table, "the round-1 stub is now a proxy"
    assert not any(p.endswith("*") or "{path" in p for _, p, _ in table), "no catch-all"


def test_each_route_forwards_to_the_service_the_contract_names() -> None:
    expected_prefix = {
        "orchestrator": ("/coding", "/validation", "/factory", "/skills", "/tickets"),
        "git_broker": ("/git/",),
        "factory_executor": ("/stations",),
        "model_manager": ("/models/",),
    }
    for route in ROUTES:
        assert route.path.startswith(expected_prefix[route.service]), route
        if route.service == "orchestrator":
            assert route.downstream_path == f"/v1{route.path}", "path preserved under /v1"
        if route.service == "factory_executor":
            assert route.downstream_path.startswith("/v1/station-records")
        assert route.path.count("{") == route.downstream_path.count("{"), route


def test_downstream_settings_default_to_the_compose_service_names(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, slas_secrets_dir=tmp_path)
    assert settings.slas_orchestrator_url == "http://agent-core-orchestrator:8000"
    assert settings.slas_git_broker_url == "http://git-broker:8000"
    assert settings.slas_sandbox_manager_url == "http://sandbox-manager:8000"
    assert settings.slas_factory_executor_url == "http://factory-executor:8000"
    assert settings.slas_model_manager_url == "http://model-manager:8000"
    downstream = Downstream.from_settings(settings)
    names = {client.service for client in downstream.clients()}
    assert names == set(DOWNSTREAM_SERVICES.values())
    assert downstream.orchestrator.base_url == "http://agent-core-orchestrator:8000"
    assert downstream.model_manager.service == "model-manager"
    downstream.close()


def test_identity_for_carries_email_name_and_every_capability() -> None:
    principal = Principal(
        "pat@slas.local",
        "Pat",
        "engineer",
        "Engineer",
        frozenset({Capability.GIT_PULL, Capability.SSH}),
    )
    identity = proxy.identity_for(principal)
    assert identity.user == "pat@slas.local" and identity.display_name == "Pat"
    assert identity.capabilities == frozenset({"git:pull", "ssh"})
    assert identity.headers()["X-Slas-Capabilities"] == "git:pull,ssh"
