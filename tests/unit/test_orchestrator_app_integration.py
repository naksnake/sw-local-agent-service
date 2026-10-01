"""The composed orchestrator app: Coding, Validation and Factory routers on one `create_app`,
runs in the registry's threads, steps to the executor services over HTTP (contract §5, §6)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from slas_hal.fakes.bmc import FakeHal, FakeTarget, Plant
from slas_http.errors import ServiceError
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_kernel.store import FileTicketStore
from slas_orchestrator.clients import HttpSandboxManager
from slas_orchestrator.service.app import RegistryRunner, create_app, route_table
from slas_orchestrator.service.runs import RunRegistry, RunStartError
from slas_orchestrator.service.settings import Settings
from slas_schemas.errors import ThreePartMessage
from slas_schemas.ticket import Ticket
from slas_validation_executor.executor import ValidationExecutor
from tests.unit.orchestrator_harness import service_client, validation_executor_app

TARGET = "lab-gx8-01"
GPU3 = "0000:8a:00.0"
PAT = Identity("pat@slas.local", "Pat Lin", frozenset({"validation:run"}))
LEE = Identity("lee@slas.local", "Lee", frozenset({"approve:destructive", "admin:people"}))
TERMINAL = {"Done", "Failed", "Needs review"}


def unreachable(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("nobody home")


class Stack:
    """`create_app` with a fake validation executor that runs the REAL executor on a FakeHal."""

    def __init__(self, tmp_path: Path, *, plants: list[Plant] | None = None) -> None:
        self.data_root = tmp_path
        self.targets = [FakeTarget(TARGET, plants=plants or [])]
        self.hal = FakeHal(self.targets, clock=FakeClock())
        self.executor = ValidationExecutor(hal=self.hal, data_root=tmp_path, clock=FakeClock())
        executor_app = validation_executor_app(self.executor, self.hal, self.targets)
        self.app = create_app(
            Settings.from_env({"SLAS_DATA_ROOT": str(tmp_path)}),
            store=FileTicketStore(tmp_path),
            sandbox=HttpSandboxManager(
                "http://sandbox-manager:8000",
                data_root=tmp_path,
                transport=httpx.MockTransport(unreachable),
            ),
            gateway=None,
            broker=None,
            probe=lambda name, _url: "ok" if name in {"gateway", "sandbox_manager"} else "down",
            validation_executor=service_client("validation-executor", executor_app),
            factory_executor=None,
            mes=None,
        )
        self.http = TestClient(self.app, raise_server_exceptions=False)

    def post(self, path: str, body: dict[str, Any], identity: Identity = PAT) -> Any:
        return self.http.post(path, json=body, headers=identity.headers())

    def get(self, path: str, identity: Identity = PAT) -> Any:
        return self.http.get(path, headers=identity.headers())

    def wait(self, ticket_id: str, timeout_s: float = 60.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            response = self.get(f"/v1/validation/runs/{ticket_id}")
            assert response.status_code == 200, response.text
            view: dict[str, Any] = response.json()
            if view["state"] in TERMINAL:
                return view
            time.sleep(0.05)
        raise AssertionError(f"{ticket_id} did not finish in {timeout_s} s")


def test_a_validation_run_goes_through_the_composed_app_in_a_thread(tmp_path: Path) -> None:
    stack = Stack(tmp_path, plants=[Plant(at_cycle=2, kind="pcie_width", bdf=GPU3, width=8)])
    health = stack.get("/health")
    assert health.status_code == 200
    assert health.json()["checks"]["validation_executor"] == "down", "optional zones stay facts"

    parsed = stack.post(
        "/v1/validation/suites/parse",
        {"filename": "gx8.md", "text": "# GX8 DC cycling\n\n- DC cycle x3, settle 60 s\n"},
    )
    assert parsed.status_code == 200, parsed.text
    suite = parsed.json()
    assert suite["problem"] is None and suite["items"][0]["cycles"] == 3

    started = stack.post("/v1/validation/runs", {"suite": suite, "target": TARGET})
    assert started.status_code == 200, started.text
    ticket_id = started.json()["ticket_id"]
    assert ticket_id == "T-validation-0001"

    view = stack.wait(ticket_id)
    assert view["state"] == "Needs review", view["sentence"]
    assert [c["status"] for c in view["cells"]] == ["ok", "finding", "finding"]
    assert view["console_tail"], "the console streamed through the executor service"
    assert view["findings"] and "PCIe" in view["findings"][0]
    assert len(stack.hal.target(TARGET).power_records) == 6

    # The Home lists see the same ticket; validation runs are the lab's, so every signed-in
    # person sees them (only coding tasks are scoped to their owner, contract §5).
    rows = stack.get("/v1/tickets", identity=LEE).json()
    by_id = {row["id"]: row for row in rows}
    assert ticket_id in by_id, "the run's ticket is on the Home list"
    assert by_id[ticket_id]["agent"] == "validation"
    assert by_id[ticket_id]["state"] == "Needs review"
    assert len(rows) == 2, "the finding spawned one bug ticket beside the run"
    stranger = Identity("sam@slas.local", "Sam", frozenset())
    assert stack.get(f"/v1/validation/runs/{ticket_id}", identity=stranger).status_code == 200
    listed = stack.get("/v1/validation/runs").json()
    assert [row["ticket_id"] for row in listed] == [ticket_id]

    # The Factory router is wired too; without an executor it answers in three parts.
    stations = stack.get("/v1/factory/stations")
    assert stations.status_code == 503
    assert stations.json()["what_happened"] == "The factory-executor did not answer."
    assert ("POST", "/v1/factory/jobs") in route_table(stack.app)


def test_the_registry_runner_turns_a_start_failure_into_three_parts(tmp_path: Path) -> None:
    registry = RunRegistry(store=FileTicketStore(tmp_path), data_root=tmp_path, log=None)
    runner = RegistryRunner(registry)

    def never_a_ticket() -> Ticket:
        raise RuntimeError("the suite could not be read")

    with pytest.raises(ServiceError) as raised:
        runner.start("job-x", never_a_ticket)
    assert raised.value.status == 400
    assert raised.value.message.what_happened == "The run stopped: the suite could not be read"

    def slow() -> Ticket:
        time.sleep(0.5)
        raise RuntimeError("late")

    with pytest.raises(RunStartError) as timed_out:
        registry.start("job-y", slow, wait_s=0.05)
    assert timed_out.value.status == 503
    assert isinstance(timed_out.value.message, ThreePartMessage)
