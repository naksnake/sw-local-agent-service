"""The orchestrator service: Coding over HTTP, skills, tickets, health, CLI (contract §5).

The sandbox manager is a second FastAPI app implementing contract §4 over a real
`SandboxManager` with `FakeSandboxRuntime`; `git` runs on the shared project directory the
way the sandbox would run it, so commits and diffs are real. The gateway is `FakeGateway`,
which scripts `EditSet`s and one consensus verdict. Tickets live in `FileTicketStore` on
`tmp_path`, which both "containers" share as `/data`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
import zipfile
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel

from slas_http.app import create_service_app
from slas_http.client import ServiceClient
from slas_http.errors import ServiceError
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext
from slas_kernel.store import FileTicketStore, MemoryTicketStore
from slas_llm_gateway.structured import SchemaViolationError, StructuredResult
from slas_llm_gateway.vllm import CompletionResponse, Message
from slas_observability import tracing
from slas_observability.events import EventLog, ListSink
from slas_orchestrator.cli import build_parser, main
from slas_orchestrator.clients import (
    HttpSandboxManager,
    TicketBoundExecutor,
    current_ticket_id,
    version_from_image,
)
from slas_orchestrator.coding.breakdown import Breakdown, LanguageChoice, TaskItem
from slas_orchestrator.coding.coder import GatewayCoder, build_messages
from slas_orchestrator.coding.executor import EditRequest, EditSet
from slas_orchestrator.service import app as app_module
from slas_orchestrator.service.app import (
    NoGatewayCoder,
    connect_gateway,
    create_app,
    http_probe,
    load_glossary,
    load_owner_routing,
    load_toolchains,
    route_table,
)
from slas_orchestrator.service.coding import breakdown_from_wire, breakdown_to_wire
from slas_orchestrator.service.deps import workspace_user
from slas_orchestrator.service.runs import RunRegistry, RunStartError, failure_sentence
from slas_orchestrator.service.settings import Settings
from slas_orchestrator.service.views import coding_task_view, ticket_row
from slas_sandbox_manager.manager import SandboxManager
from slas_sandbox_manager.runtime import ExecResult, FakeSandboxRuntime
from slas_sandbox_manager.spec import WORKSPACE
from slas_sandbox_manager.toolchains import default_manifest, detect_languages, resolve_all
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import Job
from slas_schemas.journal import JournalEntry
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import Observation, Ticket, TicketState
from slas_schemas.vote import ConsensusVerdict, Vote, VoteVerdict

REPO_ROOT = Path(__file__).resolve().parents[2]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
TERMINAL = {"Done", "Failed", "Needs review"}
EVERY_INTERFACE = "0.0.0.0"  # noqa: S104 — the container's own interface

PLAN = """# Fan controller

- Parse `config.yaml` into a dataclass in `fan_ctl.py`.
- Add a `pytest` test for the parser.

```python
import fan_ctl
```
"""

SKILL_YAML = """skill:
  id: lint-and-test
  name: Lint and test the project
  version: 1.0.0
  agents: [coding, validation]
  requires: []
  steps:
    - run: { command: ["slas-check", "lint"] }
    - run: { command: ["slas-check", "test"] }
  on_failure: stop
"""

PAT = Identity("pat@slas.local", "Pat Lin", frozenset({"git:pull"}))
ADMIN = Identity("root@slas.local", "Root", frozenset({"admin:people"}))
STRANGER = Identity("sam@slas.local", "Sam")


@pytest.fixture(autouse=True)
def _own_trace() -> Iterator[None]:
    """A direct `ServiceClient` call mints a trace id when none is bound; keep ours to ourselves."""
    with tracing.trace(tracing.new_trace_id()):
        yield


def votes(*verdicts: VoteVerdict) -> list[Vote]:
    return [
        Vote(
            voter=f"voter-{i}",
            verdict=v,
            reason="looks right" if v == "approve" else "missing a test",
            confidence=0.8,
        )
        for i, v in enumerate(verdicts, start=1)
    ]


# --- the fake sandbox manager (contract §4) ---------------------------------------------------


class FakeSandboxService:
    """Contract §4 over `SandboxManager` + `FakeSandboxRuntime`; git runs on the project dir."""

    def __init__(self, data_root: Path) -> None:
        self.runtime = FakeSandboxRuntime()
        self.clock = FakeClock(datetime(2026, 9, 17, 8, tzinfo=UTC))
        self.manager = SandboxManager(
            runtime=self.runtime, data_root=data_root, clock=self.clock, runsc_available=True
        )
        self.manifest = default_manifest()
        self.session_bodies: list[dict[str, Any]] = []
        self.execs: list[dict[str, Any]] = []
        self.runtime.handle_with(self._check)
        self.app = create_service_app(
            "sandbox-manager",
            checks=lambda: {"runtime": "ok", "isolation": "ok"},
            log=EventLog("sandbox-manager", ListSink()),
            routers=(self._router(),),
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def _project(self, user: str) -> Path:
        session = self.manager.sessions_for(user)[-1]
        return Path(session.project_dir)

    def _check(self, argv: Sequence[str], cwd: str) -> ExecResult | None:
        if list(argv[:1]) != ["slas-check"]:
            return None
        source = self._project("pat") / "fan_ctl.py"
        if argv[1] == "test":
            if source.exists() and "def parse" in source.read_text():
                return ExecResult(exit_code=0, stdout="1 passed")
            return ExecResult(exit_code=1, stderr="FAILED test_parse - AttributeError: parse")
        return ExecResult(exit_code=0, stdout=f"{argv[1]} ok")

    def _git(self, session_id: str, argv: list[str], cwd: str, stdin: str | None) -> ExecResult:
        session = self.manager.get(session_id)
        project = Path(session.project_dir)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": session.scratch_dir,
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.manager.gitconfig_path(session.user)),
        }
        directory = project if cwd == WORKSPACE else Path(cwd)
        completed = subprocess.run(
            argv, cwd=directory, env=env, input=stdin, capture_output=True, text=True, check=False
        )
        return ExecResult(
            exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
        )

    def _router(self) -> APIRouter:
        router = APIRouter(prefix="/v1")
        service = self

        @router.post("/sessions")
        def open_session(body: dict[str, Any]) -> dict[str, Any]:
            service.session_bodies.append(body)
            choices = {c["language"]: c.get("version") for c in body["languages"]}
            primary = resolve_all(choices, service.manifest)[0]
            session = service.manager.open(
                body["user"],
                body["slug"],
                image=primary.image,
                language=primary.language,
                display_name=body["display_name"],
                ttl_s=body.get("ttl_s"),
            )
            return {
                **session.model_dump(mode="json"),
                "sentence": session.sentence(service.clock.now()),
            }

        @router.get("/sessions/{session_id}")
        def get_session(session_id: str) -> dict[str, Any]:
            session = service.manager.get(session_id)
            return {**session.model_dump(mode="json"), "alive": True, "sentence": "open"}

        @router.post("/sessions/{session_id}/exec")
        def exec_session(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
            service.execs.append({"session_id": session_id, **body})
            argv = body["argv"]
            if not isinstance(argv, list):
                raise ServiceError.build(
                    400, "argv must be a list.", "A string came.", "Send argv."
                )
            cwd = body.get("cwd", WORKSPACE)
            if argv and argv[0] == "git":
                return service._git(session_id, argv, cwd, body.get("stdin")).model_dump()
            result = service.manager.exec(
                session_id, argv, cwd=cwd, timeout_s=int(body.get("timeout_s", 600))
            )
            return result.model_dump()

        @router.delete("/sessions/{session_id}", status_code=204)
        def close_session(session_id: str) -> None:
            service.manager.close(session_id)

        @router.get("/toolchains")
        def toolchains() -> dict[str, Any]:
            return {"manifest": service.manifest.toolchains, "source": service.manifest.source}

        @router.post("/toolchains/resolve")
        def resolve(body: dict[str, Any]) -> list[dict[str, Any]]:
            choices = {c["language"]: c.get("version") for c in body["choices"]}
            return [
                {**r.to_record(), "label": r.label} for r in resolve_all(choices, service.manifest)
            ]

        @router.post("/languages/detect")
        def detect(body: dict[str, Any]) -> dict[str, Any]:
            return {"languages": detect_languages(body["plan"])}

        return router

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            response = self.client.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.content,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=dict(response.headers)
            )

        return httpx.MockTransport(handler)


# --- the fake gateway ------------------------------------------------------------------------


class FakeGateway:
    """`GatewayLike`: scripted EditSets for `generate`, one verdict for `cross_check`."""

    def __init__(
        self,
        *edits: EditSet,
        verdict: ConsensusVerdict | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.edits = list(edits)
        self.verdict = verdict
        self.fail = fail
        self.generate_calls: list[tuple[str, list[Message], str]] = []
        self.cross_checks: list[tuple[str, list[Message]]] = []

    def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResponse:
        return CompletionResponse(
            instance=f"vllm-{role}", text="", prompt_tokens=1, completion_tokens=0
        )

    def generate[T: BaseModel](
        self,
        role: str,
        messages: list[Message],
        model_type: type[T],
        *,
        max_retries: int = 2,
    ) -> StructuredResult[T]:
        self.generate_calls.append((role, messages, model_type.__name__))
        if self.fail is not None:
            raise self.fail
        edits = self.edits.pop(0) if self.edits else EditSet()
        value = model_type.model_validate(edits.model_dump())
        return StructuredResult(
            value=value,
            instance=f"vllm-{role}",
            attempts=1,
            tokens=12,
            response=CompletionResponse(
                instance=f"vllm-{role}",
                text=edits.model_dump_json(),
                prompt_tokens=8,
                completion_tokens=4,
            ),
        )

    def cross_check(self, decision: str, evidence: list[Message]) -> ConsensusVerdict:
        self.cross_checks.append((decision, evidence))
        if self.verdict is None:
            raise AssertionError("no verdict scripted")
        return self.verdict.model_copy(update={"decision": decision})


def approving_verdict() -> ConsensusVerdict:
    return ConsensusVerdict(
        decision="code_change",
        rule="majority",
        votes=votes("approve", "approve", "concern"),
        agreed=True,
        sentence="2 of 3 agree with the change. voter-3 has a concern: missing a test.",
        concerns=["voter-3 has a concern: missing a test"],
    )


# --- the harness -----------------------------------------------------------------------------


def broker_transport(remotes: list[dict[str, Any]] | None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if remotes is None:
            raise httpx.ConnectError("refused")
        assert request.headers["x-slas-user"] == PAT.user
        return httpx.Response(200, json=remotes)

    return httpx.MockTransport(handler)


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        gateway: FakeGateway | None,
        remotes: list[dict[str, Any]] | None = None,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self.data_root = tmp_path
        self.sandbox_service = FakeSandboxService(tmp_path)
        self.sink = ListSink()
        self.settings = Settings.from_env(
            {
                "SLAS_DATA_ROOT": str(tmp_path),
                "SLAS_GLOSSARY": str(REPO_ROOT / "docs" / "glossary.yaml"),
                "SLAS_OWNER_ROUTING": str(REPO_ROOT / "config" / "owner-routing.yaml"),
            }
        )
        self.statuses = statuses or {
            "gateway": "ok",
            "sandbox_manager": "ok",
            "validation_executor": "down",
            "factory_executor": "down",
            "git_broker": "ok",
        }
        self.app = create_app(
            self.settings,
            store=FileTicketStore(tmp_path),
            clock=FakeClock(datetime(2026, 9, 17, 9, tzinfo=UTC)),
            log=EventLog("agent-core-orchestrator", self.sink),
            sandbox=HttpSandboxManager(
                "http://sandbox-manager:8000",
                data_root=tmp_path,
                transport=self.sandbox_service.transport(),
            ),
            gateway=gateway,
            broker=ServiceClient(
                "git-broker", "http://git-broker:8000", transport=broker_transport(remotes)
            ),
            probe=lambda name, _url: self.statuses[name],
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def get(self, path: str, identity: Identity = PAT) -> Any:
        return self.client.get(path, headers=identity.headers())

    def post(self, path: str, body: dict[str, Any], identity: Identity = PAT) -> Any:
        return self.client.post(path, json=body, headers=identity.headers())

    def delete(self, path: str, identity: Identity = PAT) -> Any:
        return self.client.delete(path, headers=identity.headers())

    def start(self, breakdown: dict[str, Any], identity: Identity = PAT) -> dict[str, Any]:
        response = self.post(
            "/v1/coding/tasks", {"breakdown": breakdown, "plan": PLAN, "filename": "plan.md"}
        )
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    def wait(self, ticket_id: str, timeout_s: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            response = self.get(f"/v1/coding/tasks/{ticket_id}")
            assert response.status_code == 200, response.text
            view: dict[str, Any] = response.json()
            if view["state"] in TERMINAL:
                return view
            time.sleep(0.05)
        raise AssertionError(f"{ticket_id} did not finish in {timeout_s} s")

    def propose(self) -> dict[str, Any]:
        response = self.post("/v1/coding/propose", {"plan": PLAN, "filename": "plan.md"})
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]


def one_task_breakdown(harness: Harness, *, cross_check: bool = True) -> dict[str, Any]:
    proposed = harness.propose()
    proposed["tasks"] = [
        {
            "n": 1,
            "title": "Parse config.yaml into a dataclass in fan_ctl.py",
            "files": ["fan_ctl.py"],
        }
    ]
    proposed["languages"] = [{"language": "python", "version": None}]
    proposed["cross_check"] = cross_check
    return proposed


# --- health and the wizard -------------------------------------------------------------------


def test_health_fails_only_on_the_mandatory_services(tmp_path: Path) -> None:
    harness = Harness(tmp_path, gateway=None)
    healthy = harness.client.get("/health")
    assert healthy.status_code == 200
    assert healthy.json()["checks"] == harness.statuses, "optional zones are reported, not fatal"
    harness.statuses["sandbox_manager"] = "down"
    sick = harness.client.get("/health")
    assert sick.status_code == 503
    assert "sandbox_manager did not answer" in sick.json()["what_happened"]
    assert harness.client.get("/metrics").status_code == 200
    assert harness.client.get("/docs").status_code == 404
    nowhere = harness.client.get("/nowhere")
    assert nowhere.status_code == 404 and "trace_id" in nowhere.json()


def test_wizard_routes_detect_propose_resolve_remotes_and_skills(tmp_path: Path) -> None:
    harness = Harness(tmp_path, gateway=None, remotes=[{"id": "r1", "name": "gitlab-firmware"}])
    assert harness.client.post("/v1/coding/propose", json={"plan": PLAN}).status_code == 401

    detected = harness.post("/v1/coding/languages/detect", {"plan": PLAN})
    assert detected.json()["languages"][0] == "python"

    proposed = harness.propose()
    assert proposed["title"] == "Fan controller"
    assert [t["title"] for t in proposed["tasks"]] == [
        "Parse `config.yaml` into a dataclass in `fan_ctl.py`.",
        "Add a `pytest` test for the parser.",
    ]
    assert proposed["export_target"] == "zip" and "export" not in proposed
    empty = harness.post("/v1/coding/propose", {"plan": "   \n"})
    assert empty.status_code == 400 and empty.json()["what_happened"] == "plan.md is empty."

    resolved = harness.post(
        "/v1/coding/toolchains/resolve",
        {"choices": [{"language": "python", "version": "3.11"}]},
    )
    assert resolved.json()[0]["version"] == "3.11.10" and resolved.json()[0]["label"] == "Python"

    assert harness.get("/v1/coding/remotes").json() == {"remotes": ["gitlab-firmware"]}
    assert harness.get("/v1/coding/skills").json() == {"skills": []}


def test_remotes_are_a_warning_when_the_broker_is_unreachable(tmp_path: Path) -> None:
    harness = Harness(tmp_path, gateway=None, remotes=None)
    answer = harness.get("/v1/coding/remotes").json()
    assert answer["remotes"] == []
    assert answer["warning"].startswith("The git-broker did not answer.")
    assert [r["event"] for r in harness.sink.records() if r["event"] == "remotes.unavailable"]


# --- end to end --------------------------------------------------------------------------------


@needs_git
def test_a_task_runs_over_http_to_done_with_zip_votes_and_feed(tmp_path: Path) -> None:
    gateway = FakeGateway(
        EditSet(files={"fan_ctl.py": "class Config: ...\n"}, note="first try"),
        EditSet(
            files={
                "fan_ctl.py": (
                    "class Config: ...\n\ndef parse(text: str) -> Config:\n    return Config()\n"
                )
            }
        ),
        verdict=approving_verdict(),
    )
    harness = Harness(tmp_path, gateway=gateway)
    breakdown = one_task_breakdown(harness)

    started = harness.start(breakdown)
    ticket_id = started["ticket_id"]
    assert ticket_id == "T-coding-0001"
    assert started["title"] == "Fan controller"
    # The route answers as soon as the ticket exists; the plan may still be on its way.
    assert started["state"] in {"Open", "Planned", "Approved", "Running", "Analysing", "Done"}

    view = harness.wait(ticket_id)
    assert view["state"] == "Done", view
    assert view["sentence"] == f"{ticket_id} is done."
    assert [s["title"] for s in view["steps"]][:2] == [
        "Toolchain: Python 3.12.6.",
        "Open an isolated sandbox for fan-controller",
    ]
    assert [s["status"] for s in view["steps"]] == ["done"] * 6
    feed = view["feed"]
    assert feed[0] == "Toolchain: Python 3.12.6."
    assert "Doing Open an isolated sandbox for fan-controller…" in feed
    assert any(line.startswith("The sandbox runs under gVisor.") for line in feed)
    assert (
        "Parse config.yaml into a dataclass in fan_ctl.py: done after 2 iterations; "
        "lint ok, type ok, test ok." in feed
    )
    assert any(line.startswith("Committed ") and f"slas/{ticket_id}" in line for line in feed)
    assert any(line.startswith("ZIP exported to ") for line in feed)
    assert "2 of 3 agree with the change. voter-3 has a concern: missing a test." in feed
    assert feed[-1].startswith("Every step finished;")

    # The ticket on disk: votes, the ZIP, the SOP in both languages.
    ticket = Ticket.model_validate_json(
        (tmp_path / "Tickets" / ticket_id / "ticket.json").read_text(encoding="utf-8")
    )
    assert ticket.user == "pat" and ticket.state is TicketState.DONE
    assert [v.voter for v in ticket.votes] == ["voter-1", "voter-2", "voter-3"]
    (export,) = ticket.exports
    artifacts = tmp_path / "Coding" / "pat" / "Artifacts" / ticket_id
    assert Path(export.path) == artifacts / "fan-controller.zip"
    with zipfile.ZipFile(export.path) as archive:
        assert archive.namelist() == ["fan_ctl.py"]
    assert ticket.sop is not None and Path(ticket.sop.zh).exists()

    # The sandbox manager saw the contract's session body and argv-only execs (no stdin).
    (session_body,) = harness.sandbox_service.session_bodies
    assert session_body["user"] == "pat" and session_body["slug"] == "fan-controller"
    assert session_body["ticket_id"] == ticket_id
    assert session_body["languages"] == [{"language": "python", "version": "3.12.6"}]
    assert session_body["display_name"] == "Pat Lin"
    commits = [
        e
        for e in harness.sandbox_service.execs
        if e["argv"][:1] == ["git"] and "commit" in e["argv"]
    ]
    assert commits and any("Slas-Ticket: " + ticket_id in part for part in commits[0]["argv"])
    assert commits[0]["argv"].count("-m") >= 2, "subject, body and trailers as -m paragraphs"
    assert all("stdin" not in e for e in commits)
    assert all(isinstance(e["argv"], list) for e in harness.sandbox_service.execs)
    assert harness.sandbox_service.runtime.created[0].runtime == "runsc"

    # The coder saw the failing check between the two iterations, never a credential.
    assert [name for _, _, name in gateway.generate_calls] == ["EditSet", "EditSet"]
    second = gateway.generate_calls[1][1][1].content
    assert "AttributeError" in second and "class Config: ..." in second
    assert gateway.cross_checks[0][0] == "code_change"

    # Lists: the person's own, newest first; an admin sees it too; a stranger does not.
    assert [t["ticket_id"] for t in harness.get("/v1/coding/tasks").json()] == [ticket_id]
    assert harness.get("/v1/coding/tasks", ADMIN).json()[0]["ticket_id"] == ticket_id
    assert harness.get("/v1/coding/tasks", STRANGER).json() == []
    assert harness.get(f"/v1/coding/tasks/{ticket_id}", STRANGER).status_code == 404

    rows = harness.get("/v1/tickets").json()
    assert rows[0]["id"] == ticket_id and rows[0]["agent"] == "coding"
    assert rows[0]["sentence"] == f"{ticket_id} is done." and rows[0]["created_at"]
    full = harness.get(f"/v1/tickets/{ticket_id}").json()
    assert full["id"] == ticket_id and full["plan"]["steps"][0]["primitive"] == "toolchain"
    assert harness.get("/v1/tickets/T-coding-9999").status_code == 404

    # Clean task: a stranger cannot remove it; the owner can once it is done. The ticket, its
    # SOP and artifacts go; the project's repository and the sandbox session's TTL are not
    # this route's business, and the run registry forgets the finished run.
    assert harness.delete(f"/v1/coding/tasks/{ticket_id}", STRANGER).status_code == 404
    still_running = make_ticket("T-coding-0042").model_copy(update={"state": TicketState.RUNNING})
    FileTicketStore(tmp_path).save(still_running)
    refused = harness.delete("/v1/coding/tasks/T-coding-0042")
    assert refused.status_code == 409
    assert (
        refused.json()["what_happened"]
        == "T-coding-0042 is still running, so it cannot be removed."
    )
    removed = harness.delete(f"/v1/coding/tasks/{ticket_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "sentence": f"{ticket_id} and its files were removed. The project's repository stays."
    }
    assert not (tmp_path / "Tickets" / ticket_id).exists()
    assert not (tmp_path / "SOP" / ticket_id).exists() and not artifacts.exists()
    assert (tmp_path / "Coding" / "pat" / "Projects" / "fan-controller").is_dir(), "the repo stays"
    assert harness.get(f"/v1/coding/tasks/{ticket_id}").status_code == 404
    assert harness.delete(f"/v1/coding/tasks/{ticket_id}").status_code == 404
    assert [t["ticket_id"] for t in harness.get("/v1/coding/tasks", ADMIN).json()] == [
        "T-coding-0042"
    ]
    assert any(e["event"] == "coding.task_removed" for e in harness.sink.records())
    (tmp_path / "Tickets" / "T-coding-0042").rename(tmp_path / "Tickets" / "gone-0042")


@needs_git
def test_a_failing_coder_ends_the_ticket_failed_with_the_sentence(tmp_path: Path) -> None:
    gateway = FakeGateway(fail=SchemaViolationError("vllm-coder", 3, "files: not a mapping"))
    harness = Harness(tmp_path, gateway=gateway)
    started = harness.start(one_task_breakdown(harness))
    ticket_id = started["ticket_id"]
    view = harness.wait(ticket_id)
    assert view["state"] == "Failed"
    sentence = (
        "vllm-coder did not produce a valid answer in 3 attempts. "
        "Its last answer did not match the required schema: files: not a mapping"
    )
    assert view["sentence"] == f"{ticket_id} failed: {sentence}"
    assert view["feed"][-1] == sentence
    statuses = [s["status"] for s in view["steps"]]
    assert statuses[:2] == ["done", "done"] and statuses[2] == "running", (
        "the interrupted step keeps its open intent; the ticket says why it stopped"
    )
    failed = [r for r in harness.sink.records() if r["event"] == "run.failed"]
    assert failed and failed[0]["error_type"] == "SchemaViolationError"
    registry_state = harness.app.state.deps.registry.state_of(ticket_id)
    assert registry_state is not None and registry_state.error == sentence
    assert harness.get("/v1/tickets").json()[0]["sentence"].endswith(sentence)


def test_start_task_refuses_a_bad_breakdown_or_language(tmp_path: Path) -> None:
    harness = Harness(tmp_path, gateway=None)
    proposed = harness.propose()
    bad = harness.post("/v1/coding/tasks", {"breakdown": {**proposed, "tasks": []}, "plan": PLAN})
    assert bad.status_code == 400
    assert bad.json()["what_happened"].startswith("The breakdown could not be used")
    unknown = {**proposed, "languages": [{"language": "cobol", "version": None}]}
    refused = harness.post("/v1/coding/tasks", {"breakdown": unknown, "plan": PLAN})
    assert refused.status_code == 400
    assert "not a language the Coding Agent knows" in refused.json()["what_happened"]


# --- skills -----------------------------------------------------------------------------------


def test_skills_import_enable_list_export_and_the_wizard_list(tmp_path: Path) -> None:
    harness = Harness(tmp_path, gateway=None)
    assert harness.get("/v1/skills").json() == []

    imported = harness.post("/v1/skills/import", {"yaml": SKILL_YAML})
    assert imported.status_code == 200, imported.text
    report = imported.json()
    assert report["skill"]["id"] == "lint-and-test" and report["risk"] == "caution"
    assert report["sentence"].startswith("Lint and test the project v1.0.0: 2 steps")
    assert report["state_sentence"] == "Off for every agent."
    assert (tmp_path / "Skills" / "library" / "lint-and-test.skill.yaml").exists()

    (row,) = harness.get("/v1/skills").json()
    assert row["id"] == "lint-and-test"
    assert row["enabled"] == {"coding": False, "validation": False}
    assert row["agents"] == ["coding", "validation"] and row["requires"] == []
    assert harness.get("/v1/coding/skills").json() == {"skills": []}

    enabled = harness.post("/v1/skills/lint-and-test/enable", {"agent": "coding"})
    assert enabled.status_code == 200 and enabled.json()["enabled"]["coding"] is True
    assert enabled.json()["sentence"].startswith("On for the Coding Agent since")
    assert harness.get("/v1/coding/skills").json() == {
        "skills": [{"id": "lint-and-test", "name": "Lint and test the project"}]
    }
    refused = harness.post("/v1/skills/lint-and-test/enable", {"agent": "factory"})
    assert refused.status_code == 400
    assert "can't be turned on for the Factory Agent" in refused.json()["what_happened"]

    disabled = harness.post("/v1/skills/lint-and-test/disable", {"agent": "coding"})
    assert disabled.json()["enabled"]["coding"] is False

    exported = harness.get("/v1/skills/lint-and-test/export").json()
    assert exported["yaml"].startswith("# Lint and test the project — skill lint-and-test v1.0.0")
    assert re.fullmatch(r"[0-9a-f]{64}", exported["content_hash"])
    assert "state" not in exported["yaml"]

    missing = harness.get("/v1/skills/nope/export")
    assert missing.status_code == 404
    assert missing.json()["what_happened"].startswith("There is no skill nope")
    broken = harness.post("/v1/skills/import", {"yaml": "skill: [unclosed"})
    assert broken.status_code == 400
    assert broken.json()["what_happened"] == "The skill file is not valid YAML."
    shell = SKILL_YAML.replace(
        'run: { command: ["slas-check", "lint"] }', 'shell: { command: "rm -rf /" }'
    )
    rejected = harness.post("/v1/skills/import", {"yaml": shell})
    assert rejected.status_code == 400
    assert "not a skill primitive" in rejected.json()["what_happened"]
    lacking = harness.post(
        "/v1/skills/import", {"yaml": SKILL_YAML.replace("requires: []", "requires: [screen]")}
    )
    assert lacking.status_code == 400
    assert lacking.json()["what_happened"].startswith("You can't import")
    (tmp_path / "Skills" / "library" / "junk.skill.yaml").write_text("not: a skill\n")
    assert [r["id"] for r in harness.get("/v1/skills").json()] == ["lint-and-test"]


# --- the contract and the CLI -----------------------------------------------------------------


def test_route_table_matches_the_contract(tmp_path: Path) -> None:
    contract = (REPO_ROOT / "docs" / "api-contract-round-2.md").read_text(encoding="utf-8")
    section = contract.split("## 5. agent-core-orchestrator", 1)[1].split("\n## 6.", 1)[0]
    documented = {
        (method, path)
        for method, path in re.findall(r"`(GET|POST|PUT|DELETE) (/v1/[^\s`]+)`", section)
        if path.startswith(("/v1/coding", "/v1/skills", "/v1/tickets"))
    }
    documented.add(("POST", "/v1/skills/{id}/disable"))  # written "`…/enable` · `/disable`"
    app = create_app(
        Settings.from_env({"SLAS_DATA_ROOT": str(tmp_path)}), gateway=None, broker=None
    )
    served = {
        (method, path)
        for method, path in route_table(app)
        if path.startswith(("/v1/coding", "/v1/skills", "/v1/tickets"))
    }
    assert served == documented
    assert ("GET", "/health") in route_table(app)
    # The Validation and Factory routers are wired too (their own tests hold them to §5).
    assert ("POST", "/v1/validation/runs") in route_table(app)
    assert ("POST", "/v1/factory/jobs") in route_table(app)


def test_cli_parses_serve_and_prints_routes(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = build_parser().parse_args(["serve", "--bind", "127.0.0.1:9000"])
    assert args.command == "serve" and args.bind == "127.0.0.1:9000"
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    monkeypatch.setenv("SLAS_DATA_ROOT", str(tmp_path))
    assert main(["routes"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "POST /v1/coding/tasks" in lines and "GET /health" in lines


def test_app_wiring_helpers_degrade_with_a_sentence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink = ListSink()
    log = EventLog("t", sink)
    settings = Settings.from_env({"SLAS_DATA_ROOT": str(tmp_path), "SLAS_GATEWAY_URL": "http://gw"})

    # No gateway client module → None and a warning; a module with HttpGateway → connected.
    monkeypatch.setattr(app_module, "GATEWAY_CLIENT_MODULE", "slas_llm_gateway.no_such_client")
    assert connect_gateway(settings, log) is None
    assert [r["event"] for r in sink.records()] == ["gateway.client_missing"]
    fake_module = types.ModuleType("fake_gateway_client")
    built: list[str] = []

    class HttpGateway:
        def __init__(self, base_url: str) -> None:
            built.append(base_url)

    fake_module.HttpGateway = HttpGateway  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_gateway_client", fake_module)
    monkeypatch.setattr(app_module, "GATEWAY_CLIENT_MODULE", "fake_gateway_client")
    assert isinstance(connect_gateway(settings, log), HttpGateway) and built == ["http://gw"]

    # The probe: 200 is ok, anything else or no answer is down.
    answers: dict[str, Any] = {"http://up/health": 200, "http://sick/health": 503}

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        if url not in answers:
            raise httpx.ConnectError("refused")
        return httpx.Response(answers[url])

    monkeypatch.setattr(httpx, "get", fake_get)
    probe = http_probe(2.0)
    assert probe("a", "http://up/") == "ok"
    assert probe("b", "http://sick") == "down" and probe("c", "http://gone") == "down"

    # Unreadable configuration files fall back and say so, instead of stopping the service.
    bad = tmp_path / "bad.yaml"
    bad.write_text("terms: [1, 2\n")
    assert load_glossary(bad, log).lookup("baseline") is not None
    assert load_owner_routing(bad, log) is None
    assert load_owner_routing(tmp_path / "absent.yaml", log) is None
    (tmp_path / "Toolchains").mkdir()
    (tmp_path / "Toolchains" / "manifest.json").write_text("{not json")
    assert load_toolchains(tmp_path, log).newest("python") == "3.12.6"
    events = [r["event"] for r in sink.records()]
    assert {"glossary.unreadable", "owner_routing.unreadable", "toolchains.unreadable"} <= set(
        events
    )

    # A person who is nobody in particular still gets a usable app; no gateway → no coder.
    app = create_app(settings, gateway=None, broker=None)
    assert isinstance(app.state.deps.coding_executor.coder, NoGatewayCoder)
    assert app.state.deps.coding_executor.cross_checker is None


def test_settings_read_the_environment_with_defaults() -> None:
    settings = Settings.from_env({"SLAS_GATEWAY_URL": "http://gw:1", "SLAS_BIND": " "})
    assert settings.gateway_url == "http://gw:1"
    assert settings.sandbox_manager_url == "http://sandbox-manager:8000"
    assert settings.bind == f"{EVERY_INTERFACE}:8000"
    assert settings.data_root == Path("/data")
    assert settings.skills_library == Path("/data/Skills/library")
    assert list(settings.service_urls()) == [
        "gateway",
        "sandbox_manager",
        "validation_executor",
        "factory_executor",
        "git_broker",
    ]


# --- units: registry, clients, views, coder ---------------------------------------------------


def make_ticket(ticket_id: str = "T-coding-0001", user: str = "pat") -> Ticket:
    now = datetime(2026, 9, 17, tzinfo=UTC)
    return Ticket(
        id=ticket_id,
        agent="coding",
        user=user,
        title="Fan controller",
        job=Job(id="job-1", agent="coding", user=user, title="Fan controller", created_at=now),
        created_at=now,
        updated_at=now,
    )


def test_registry_survives_a_run_that_raises_after_the_ticket_exists(tmp_path: Path) -> None:
    store = MemoryTicketStore()
    registry = RunRegistry(
        store=store, data_root=tmp_path, clock=FakeClock(), log=EventLog("t", ListSink())
    )

    def run() -> Ticket:
        ticket = make_ticket()
        registry.store.save(ticket)
        ticket.transition(TicketState.PLANNED, datetime.now(UTC), "planned")
        registry.store.save(ticket)
        raise RuntimeError("the sandbox vanished")

    state = registry.start("job-1", run)
    assert state.ticket_id == "T-coding-0001"
    assert state.thread is not None
    state.thread.join(5)
    assert not state.running and state.error == "The run stopped: the sandbox vanished"
    ticket = store.load("T-coding-0001")
    assert ticket.state is TicketState.FAILED
    assert ticket.history[-1].reason == "The run stopped: the sandbox vanished"
    journal = tmp_path / "Tickets" / "T-coding-0001" / "journal.jsonl"
    entries = [json.loads(line) for line in journal.read_text().splitlines()]
    assert entries[-1]["kind"] == "state" and entries[-1]["payload"]["to"] == "Failed"
    assert registry.state_of("T-coding-0001") is state and registry.running_ids() == []


def test_registry_reports_a_run_that_fails_before_any_ticket_and_a_slow_one(
    tmp_path: Path,
) -> None:
    registry = RunRegistry(store=MemoryTicketStore(), data_root=tmp_path)

    def early() -> Ticket:
        raise ValueError("the plan is empty")

    with pytest.raises(RunStartError) as raised:
        registry.start("job-2", early)
    assert raised.value.status == 400
    assert raised.value.message.what_happened == "The run stopped: the plan is empty"

    release = threading.Event()

    def slow() -> Ticket:
        release.wait(5)
        return make_ticket("T-coding-0002")

    with pytest.raises(RunStartError) as timed_out:
        registry.start("job-3", slow, wait_s=0.05)
    assert timed_out.value.status == 503
    release.set()
    # A resume names its ticket up front and needs no wait.
    done = registry.start("job-4", lambda: make_ticket("T-coding-0003"), ticket_id="T-coding-0003")
    assert done.ticket_id == "T-coding-0003"


def test_failure_sentences_and_the_no_gateway_coder() -> None:
    assert failure_sentence(RuntimeError("")) == "The run stopped with RuntimeError."
    assert failure_sentence(KeyError("x")) == "The run stopped: 'x'"
    with pytest.raises(RuntimeError) as raised:
        NoGatewayCoder().propose_edits(
            EditRequest(task=TaskItem(n=1, title="t"), iteration=1, languages=[], files={})
        )
    assert failure_sentence(raised.value).startswith("The Coding Agent cannot propose edits")
    # A three-part message keeps its cause: for a runtime refusal it is the only part that
    # names the problem, and the person reads it on the task card, not in a service log.
    refused = ServiceError(
        502,
        ThreePartMessage(
            "The container runtime refused to create slas-sbx-x from local/slas/sandbox-python.",
            "It answered 400: unknown or invalid runtime name: runsc.",
            "Read the message above; `slas logs sandbox-manager` on the host has it all.",
        ),
    )
    assert failure_sentence(refused) == (
        "The container runtime refused to create slas-sbx-x from local/slas/sandbox-python. "
        "It answered 400: unknown or invalid runtime name: runsc."
    )
    repeated = ServiceError(500, ThreePartMessage("It broke. Because so.", "Because so.", "Fix."))
    assert failure_sentence(repeated) == "It broke. Because so.", (
        "a cause already said is not repeated"
    )


def test_http_sandbox_manager_paths_bodies_and_the_ticket_binding(tmp_path: Path) -> None:
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    fake = FakeSandboxService(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": "ok"})
        session = fake.manager.open(
            "pat", "demo", image="img:1", language="python", display_name="Pat"
        )
        payload = {**session.model_dump(mode="json"), "sentence": "open", "alive": True}
        return httpx.Response(200, json=payload)

    client = HttpSandboxManager(
        "http://sandbox-manager:8000", data_root=tmp_path, transport=httpx.MockTransport(handler)
    )
    coding = tmp_path / "Coding" / "pat"
    assert client.project_dir("pat", "demo") == coding / "Projects" / "demo"
    assert client.artifacts_dir("pat", "T-1") == coding / "Artifacts" / "T-1"
    assert client.gitconfig_path("pat") == coding / "gitconfig"
    assert client.prepare_project("pat", "demo", display_name="Pat").is_dir()

    token = current_ticket_id.set("T-coding-0007")
    try:
        session = client.open(
            "pat",
            "demo",
            image="registry/x:3.12.6",
            language="python",
            display_name="Pat",
            ttl_s=900,
        )
    finally:
        current_ticket_id.reset(token)
    assert session.slug == "demo"
    assert seen[-1][2] == {
        "user": "pat",
        "slug": "demo",
        "display_name": "Pat",
        "languages": [{"language": "python", "version": "3.12.6"}],
        "ticket_id": "T-coding-0007",
        "ttl_s": 900,
    }
    client.open("pat", "demo", image="registry/x@sha256:abc", language="python", display_name="P")
    digest_body = seen[-1][2] or {}
    assert "ticket_id" not in digest_body and digest_body["languages"][0]["version"] is None
    assert client.session("s1").slug == "demo"

    result = client.exec("s1", ["git", "commit", "--file=-"], stdin="msg\n", timeout_s=30)
    assert result.exit_code == 0 and result.stdout == "ok"
    assert seen[-1] == (
        "POST",
        "/v1/sessions/s1/exec",
        {
            "argv": ["git", "commit", "--file=-"],
            "cwd": WORKSPACE,
            "timeout_s": 30,
            "stdin": "msg\n",
        },
    )
    client.exec("s1", ["ls"])
    assert "stdin" not in (seen[-1][2] or {})
    with pytest.raises(ValueError, match="argv must be"):
        client.exec("s1", [])
    client.close_session("s1")
    assert seen[-1][:2] == ("DELETE", "/v1/sessions/s1")
    client.close()

    assert version_from_image("registry.internal/slas/sandbox-python:3.12.6") == "3.12.6"
    assert version_from_image("localhost:5000/sandbox-python:3.12.6") == "3.12.6"
    assert version_from_image("sandbox-python") is None

    class Recording:
        def execute(self, step: Step, context: ExecutionContext) -> Observation:
            return Observation(exit_code=0, summary=str(current_ticket_id.get()))

    bound = TicketBoundExecutor(Recording())
    context = ExecutionContext(ticket_id="T-coding-0009", job_id="j", agent="coding", user="pat")
    step = Step(id="s", n=1, primitive="fake", title="t")
    assert bound.execute(step, context).summary == "T-coding-0009"
    assert current_ticket_id.get() is None


def entry(seq: int, kind: str, **fields: Any) -> JournalEntry:
    now = datetime(2026, 9, 17, tzinfo=UTC)
    return JournalEntry(seq=seq, at=now, ticket_id="T-coding-0001", kind=kind, **fields)


def test_views_for_a_ticket_without_plan_and_for_every_state() -> None:
    ticket = make_ticket()
    view = coding_task_view(ticket, None, [], None)
    assert view == {
        "ticket_id": "T-coding-0001",
        "title": "Fan controller",
        "state": "Open",
        "sentence": "T-coding-0001 is open.",
        "steps": [],
        "feed": [],
    }
    toolchain = "Toolchain: Python 3.12.6."
    plan = Plan(
        id="plan-1",
        job_id="job-1",
        summary="two steps",
        steps=[
            Step(
                id="toolchain",
                n=1,
                primitive="toolchain",
                title=toolchain,
                args={"sentence": toolchain},
            ),
            Step(id="sandbox", n=2, primitive="sandbox_open", title="Open a sandbox"),
        ],
        created_at=datetime(2026, 9, 17, tzinfo=UTC),
    )
    gate = {"what_happened": "The skill x is not in the library."}
    entries = [
        entry(1, "state", payload={"reason": "ingested"}),
        entry(2, "intent", step_id="toolchain"),
        entry(3, "observation", step_id="toolchain", payload={"summary": toolchain}),
        entry(4, "intent", step_id="sandbox", payload={"note": "re-performing after a crash"}),
        entry(5, "observation", step_id="sandbox", payload={"summary": ""}),
        entry(6, "note", payload={"skill_gate": gate, "plan_cross_check": "3 of 3 agree."}),
        entry(7, "state", payload={"reason": "Starting the steps."}),
    ]
    running = ticket.model_copy(update={"state": TicketState.RUNNING})
    view = coding_task_view(running, plan, entries, None)
    assert view["feed"] == [
        toolchain,
        "Doing Open a sandbox… (re-performing after a crash)",
        "The skill x is not in the library.",
        "3 of 3 agree.",
        "Starting the steps.",
    ]
    assert view["steps"] == [
        {"n": 1, "title": toolchain, "status": "pending"},
        {"n": 2, "title": "Open a sandbox", "status": "pending"},
    ]
    assert view["sentence"] == "T-coding-0001 is running: 0 of 2 steps done."
    failed = ticket.model_copy(update={"state": TicketState.FAILED})
    assert coding_task_view(failed, plan, [], None)["sentence"] == (
        "T-coding-0001 failed: the run stopped."
    )
    review = ticket.model_copy(update={"state": TicketState.NEEDS_REVIEW, "plan": plan})
    assert ticket_row(review)["sentence"] == (
        "T-coding-0001 needs your review: 0 of 2 steps finished."
    )


def test_breakdown_wire_adapters_and_workspace_user() -> None:
    breakdown = Breakdown(
        title="T",
        tasks=[TaskItem(n=1, title="a")],
        languages=[LanguageChoice(language="python")],
        export="bundle",
    )
    wire = breakdown_to_wire(breakdown)
    assert wire["export_target"] == "bundle" and "export" not in wire
    assert breakdown_from_wire(wire) == breakdown
    assert breakdown_from_wire({**wire, "export_target": "zip"}).export == "zip"
    with pytest.raises(ServiceError) as raised:
        breakdown_from_wire({**wire, "export_target": "remote"})
    assert raised.value.status == 400
    assert "needs the name of a saved remote" in raised.value.message.what_happened
    assert workspace_user(Identity("Pat.Lin@Example.com")) == "pat.lin"
    assert workspace_user(Identity("weird name!")) == "weird-name"
    assert workspace_user(Identity("@@")) == "user"


def test_gateway_coder_builds_a_bounded_prompt_without_secrets() -> None:
    request = EditRequest(
        task=TaskItem(n=2, title="Add parse", files=["fan_ctl.py"], acceptance="pytest passes"),
        iteration=2,
        languages=["python"],
        files={"fan_ctl.py": "class Config: ...\n", "big.txt": "x" * 200_000, "z.txt": "zz"},
        failures=[],
    )
    messages = build_messages(request)
    assert messages[0].role == "system" and "JSON object" in messages[0].content
    user = messages[1].content
    assert user.startswith("Task 2: Add parse\nLanguages: python\nIteration: 2\n")
    assert "Files the plan names: fan_ctl.py" in user and "Acceptance: pytest passes" in user
    assert "Every check passed." in user and "--- fan_ctl.py ---" in user
    assert "not shown to keep the prompt short: big.txt" in user and len(user) < 130_000
    first = build_messages(request.model_copy(update={"iteration": 1, "files": {}}))[1].content
    assert "No check has run yet." in first and "The project is empty." in first
    gateway = FakeGateway(EditSet(files={"a.py": "x = 1\n"}, note="n"))
    coder = GatewayCoder(gateway)
    edits = coder.propose_edits(request)
    assert edits.files == {"a.py": "x = 1\n"} and coder.last_attempts == 1
    assert gateway.generate_calls[0][0] == "coder"
