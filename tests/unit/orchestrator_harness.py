"""Fake executor *services* for the orchestrator's Validation and Factory routers.

Each fake is a real FastAPI app built with `create_service_app` that implements the executor
routes of docs/api-contract-round-2.md §6 over the REAL `ValidationExecutor` (FakeHal) and
`FactoryExecutor` (FakeStation), so `HttpExecutor` and `ExecutorReader` are exercised end to
end over HTTP: the kernel's step leaves the process as JSON and comes back as an observation.
`ServiceClient` is synchronous, so the transport is an `httpx.MockTransport` that dispatches
into a `TestClient` of the fake app.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from slas_factory_executor.executor import FactoryExecutor
from slas_factory_executor.templates import TestLoopTemplate
from slas_hal.fakes.bmc import FakeHal, FakeTarget
from slas_http.app import create_service_app
from slas_http.client import ServiceClient
from slas_http.errors import ServiceError
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import MesTicket
from slas_schemas.plan import Step
from slas_schemas.ticket import Ticket
from slas_station_runner.fakes import FakeStation
from slas_station_runner.protocol import BatchError
from slas_validation_executor.executor import RunState, ValidationExecutor

_HOP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}


def bridge(app: FastAPI) -> httpx.MockTransport:
    """An httpx transport that hands every request to the fake app's TestClient."""
    client = TestClient(app, raise_server_exceptions=False)

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        response = client.request(
            request.method,
            request.url.raw_path.decode("ascii"),
            content=request.content,
            headers=headers,
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    return httpx.MockTransport(handler)


def service_client(service: str, app: FastAPI) -> ServiceClient:
    return ServiceClient(service, f"http://{service}:8000", transport=bridge(app))


class SyncRunner:
    """The tests' `Runner`: the run happens in the calling thread, at once."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.tickets: list[Ticket] = []

    def start(self, key: str, fn: Callable[[], Ticket]) -> None:
        self.started.append(key)
        self.tickets.append(fn())


def _execute_body(body: dict[str, Any]) -> tuple[Step, ExecutionContext]:
    return Step.model_validate(body["step"]), ExecutionContext.model_validate(body["context"])


def _unknown(exc: UnknownPrimitiveError) -> ServiceError:
    return ServiceError(
        400,
        ThreePartMessage(
            str(exc),
            "The plan names a verb this executor does not perform.",
            "Recompile the plan against plans/primitives; nothing ran.",
        ),
    )


def _not_found(noun: str, ticket_id: str) -> ServiceError:
    return ServiceError(
        404,
        ThreePartMessage(
            f"There is no {noun} {ticket_id} on this executor.",
            "No step of it has been performed here yet.",
            "Open the run to see where it is.",
        ),
    )


# --- validation-executor --------------------------------------------------------------------------


def validation_executor_app(
    executor: ValidationExecutor, hal: FakeHal, targets: list[FakeTarget]
) -> FastAPI:
    router = APIRouter(prefix="/v1")

    @router.post("/execute")
    def execute(body: dict[str, Any]) -> dict[str, Any]:
        step, context = _execute_body(body)
        try:
            observation = executor.execute(step, context)
        except UnknownPrimitiveError as exc:
            raise _unknown(exc) from exc
        return observation.model_dump(mode="json")

    def _states() -> list[RunState]:
        runs = executor.data_root / "Validation" / "Runs"
        if not runs.is_dir():
            return []
        return [
            RunState.model_validate_json((child / "cycles.json").read_text(encoding="utf-8"))
            for child in sorted(runs.iterdir())
            if (child / "cycles.json").is_file()
        ]

    @router.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        return [{"ticket_id": s.ticket_id, "state": s.model_dump(mode="json")} for s in _states()]

    @router.get("/runs/{ticket_id}")
    def run(ticket_id: str) -> dict[str, Any]:
        state = executor.state_for(ticket_id)
        if state is None:
            raise _not_found("run", ticket_id)
        console = executor.run_dir(ticket_id) / "console.log"
        tail = console.read_text(encoding="utf-8").splitlines()[-40:] if console.is_file() else []
        return {**state.model_dump(mode="json"), "console_tail": tail}

    @router.get("/targets")
    def list_targets() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        now = executor.clock.now()
        for target in targets:
            lease = executor.leases.holder(target.ref, now)
            rows.append(
                {
                    "ref": target.ref,
                    "model": hal.inventory(target.ref).model,
                    "free": lease is None,
                    "holder": lease.sentence() if lease else None,
                    "armed": True,
                    "sentence": f"{target.ref}: {hal.inventory(target.ref).model}; "
                    + (lease.sentence() if lease else "free."),
                }
            )
        return rows

    return create_service_app("validation-executor", routers=(router,))


# --- factory-executor -----------------------------------------------------------------------------


def factory_executor_app(
    executor: FactoryExecutor,
    stations: dict[str, FakeStation],
    templates: dict[str, TestLoopTemplate],
    pending: list[MesTicket],
) -> FastAPI:
    router = APIRouter(prefix="/v1")

    @router.post("/execute")
    def execute(body: dict[str, Any]) -> dict[str, Any]:
        step, context = _execute_body(body)
        try:
            observation = executor.execute(step, context)
        except UnknownPrimitiveError as exc:
            raise _unknown(exc) from exc
        return observation.model_dump(mode="json")

    def _state_or_404(ticket_id: str) -> Any:
        state = executor.state_for(ticket_id)
        if state is None:
            raise _not_found("job", ticket_id)
        return state

    @router.get("/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        jobs = executor.data_root / "Factory" / "Jobs"
        rows: list[dict[str, Any]] = []
        if jobs.is_dir():
            for child in sorted(jobs.iterdir()):
                path = child / "state.json"
                if path.is_file():
                    state = json.loads(path.read_text(encoding="utf-8"))
                    rows.append({"ticket_id": state["ticket_id"], "state": state})
        return rows

    @router.get("/jobs/{ticket_id}")
    def job(ticket_id: str) -> dict[str, Any]:
        return dict(_state_or_404(ticket_id).model_dump(mode="json"))

    @router.post("/jobs/{ticket_id}/control")
    def control(ticket_id: str, body: dict[str, Any]) -> dict[str, Any]:
        state = _state_or_404(ticket_id)
        try:
            result = executor.control(ticket_id, body["verb"], by=str(body.get("by", "")))
        except BatchError as exc:
            raise ServiceError(409, exc.message) from exc
        station = stations[state.station]
        vnc = station.runner.config.vnc
        answer = result.model_dump(mode="json")
        if vnc.enabled:
            answer["watch_url"] = (
                f"vnc://127.0.0.1:5901 (relayed over mTLS to https://{state.station}:8443)"
            )
            answer["watch_problem"] = None
        else:
            answer["watch_url"] = None
            answer["watch_problem"] = (
                f"VNC is not enabled on {state.station}. The station record has VNC off. "
                "Enable it under Admin → Stations and re-enrol."
            )
        return answer

    @router.post("/jobs/{ticket_id}/decide")
    def decide(ticket_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _state_or_404(ticket_id)
        state = executor.decide(
            ticket_id, verdict=body["verdict"], by=str(body["by"]), note=str(body.get("note", ""))
        )
        return dict(state.model_dump(mode="json"))

    @router.get("/stations")
    def list_stations() -> list[dict[str, Any]]:
        now = executor.clock.now()
        rows: list[dict[str, Any]] = []
        for name in sorted(stations):
            lease = executor.leases.holder(name, now)
            rows.append(
                {
                    "name": name,
                    "description": f"Fake station {name}",
                    "free": lease is None,
                    "holder": lease.sentence() if lease else None,
                    "enrolled": True,
                    "sentence": f"{name}: " + (lease.sentence() if lease else "free."),
                }
            )
        return rows

    @router.get("/templates")
    def list_templates() -> list[dict[str, Any]]:
        return [
            {**t.model_dump(mode="json"), "sentence": t.sentence()}
            for _, t in sorted(templates.items())
        ]

    @router.get("/mes/pending")
    def mes_pending() -> list[dict[str, Any]]:
        return [t.model_dump(mode="json") for t in pending]

    return create_service_app("factory-executor", routers=(router,))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
