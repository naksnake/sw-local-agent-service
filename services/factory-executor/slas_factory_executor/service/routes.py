"""The factory executor's routes (docs/api-contract-round-2.md §6).

POST /v1/execute                          one step for the kernel → Observation
GET  /v1/jobs · /v1/jobs/{id}             JobState rows
POST /v1/jobs/{id}/control                pause · resume · abort · status; factory:control
POST /v1/jobs/{id}/decide                 the line lead's PASS or FAIL; factory:verdict
GET  /v1/stations                         the registry joined with the station leases
GET/POST /v1/station-records, PUT …/{name}/tuning, POST …/{name}/code, POST …/{name}/revoke,
DELETE …/{name}                           Admin → Stations; factory:stations_manage
GET  /v1/templates                        Factory/Templates (the shipped one copied there)
GET  /v1/mes/pending                      production tickets picked up, not yet reported
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from slas_factory_executor.executor import JobState
from slas_factory_executor.service.collaborators import (
    ensure_shipped_templates,
    pending_tickets,
    templates_in,
)
from slas_factory_executor.stations import StationError, StationRecord
from slas_hal.credentials import CredentialError
from slas_http.errors import ServiceError
from slas_http.identity import identity_of, require
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.leases import Lease, LeaseError
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Step
from slas_screen.retention import RetentionPolicy
from slas_station_runner.protocol import BatchError
from slas_station_runner.runner import ScreenTuning

CONTROL_CAPABILITY: Final = "factory:control"
VERDICT_CAPABILITY: Final = "factory:verdict"
STATIONS_CAPABILITY: Final = "factory:stations_manage"

router = APIRouter(prefix="/v1")


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: Step
    context: ExecutionContext


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verb: Literal["pause", "resume", "abort", "status"]
    by: str = ""


class DecideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["PASS", "FAIL"]
    by: str = ""
    note: str = ""


class NewStationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    description: str = ""


class TuningBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen: ScreenTuning | None = None
    retention: RetentionPolicy | None = None
    vnc_enabled: bool | None = None


def _services(request: Request) -> Any:
    return request.app.state.services


# --- errors ------------------------------------------------------------------------------------


def problem_for(exc: Exception) -> ServiceError | None:
    """The three-part answer for an exception the executor let through, or None for a bug."""
    if isinstance(exc, UnknownPrimitiveError):
        return ServiceError(
            400,
            ThreePartMessage(
                str(exc),
                "The plan names a verb outside plans/primitives/factory.yaml.",
                "Recompile the plan from its template; the compiler rejects unknown verbs.",
            ),
        )
    if isinstance(exc, LeaseError):
        return ServiceError(409, exc.message)
    if isinstance(exc, StationError):
        return ServiceError(404, exc.message)
    if isinstance(exc, BatchError | CredentialError):
        return ServiceError(502, exc.message)
    return None


def _no_such_job(ticket_id: str) -> ServiceError:
    return ServiceError(
        404,
        ThreePartMessage(
            f"There is no factory job for {ticket_id}.",
            "No step has run for that ticket on this executor, or the id is mistyped.",
            "Open the job from the Factory page; it appears once its first step starts.",
        ),
    )


def _job_row(state: JobState) -> dict[str, Any]:
    return {**state.model_dump(mode="json"), "sentence": state.sentence()}


# --- execute -----------------------------------------------------------------------------------


@router.post("/execute")
def execute(request: Request, body: ExecuteRequest) -> dict[str, Any]:
    svc = _services(request)
    with svc.locks.hold(body.context.ticket_id):
        try:
            observation = svc.executor.execute(body.step, body.context)
        except Exception as exc:
            problem = problem_for(exc)
            if problem is None:
                raise
            svc.log.warning(
                "execute.refused",
                ticket_id=body.context.ticket_id,
                step=body.step.id,
                status=problem.status,
                what_happened=problem.message.what_happened,
            )
            raise problem from None
    svc.log.info(
        "execute.done",
        ticket_id=body.context.ticket_id,
        step=body.step.id,
        primitive=body.step.primitive,
        exit_code=observation.exit_code,
    )
    return dict(observation.model_dump(mode="json"))


# --- jobs ---------------------------------------------------------------------------------------


def _job_ids(jobs_dir: Path) -> list[str]:
    """Newest first, by the state file's modification time."""
    states = sorted(jobs_dir.glob("*/state.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    return [path.parent.name for path in states]


@router.get("/jobs")
def jobs(request: Request) -> list[dict[str, Any]]:
    svc = _services(request)
    rows: list[dict[str, Any]] = []
    for ticket_id in _job_ids(svc.executor.data_root / "Factory" / "Jobs"):
        state = svc.executor.state_for(ticket_id)
        if state is not None:
            rows.append(_job_row(state))
    return rows


@router.get("/jobs/{id}")
def job(request: Request, id: str) -> dict[str, Any]:
    svc = _services(request)
    state = svc.executor.state_for(id)
    if state is None:
        raise _no_such_job(id)
    return _job_row(state)


@router.post("/jobs/{id}/control")
def control(request: Request, id: str, body: ControlBody) -> dict[str, Any]:
    svc = _services(request)
    identity = identity_of(request)
    require(identity, CONTROL_CAPABILITY, verb=f"take over the station of {id}")
    by = body.by or identity.name
    state = svc.executor.state_for(id)
    if state is None:
        raise _no_such_job(id)
    try:
        result = svc.executor.control(id, body.verb, by=by)
    except BatchError as exc:
        raise ServiceError(502, exc.message) from None
    except CredentialError as exc:
        raise ServiceError(502, exc.message) from None
    try:
        record: StationRecord | None = svc.registry.get(state.station)
    except StationError:
        record = None
    watch_url, watch_problem = svc.watcher.watch(record, state.station)
    svc.log.info("job.control", ticket_id=id, verb=body.verb, by=by)
    return {
        **result.model_dump(mode="json"),
        "watch_url": watch_url,
        "watch_problem": watch_problem,
    }


@router.post("/jobs/{id}/decide")
def decide(request: Request, id: str, body: DecideBody) -> dict[str, Any]:
    svc = _services(request)
    identity = identity_of(request)
    require(identity, VERDICT_CAPABILITY, verb=f"decide the verdict of {id}")
    by = body.by or identity.name
    try:
        state = svc.executor.decide(id, verdict=body.verdict, by=by, note=body.note)
    except KeyError:
        raise _no_such_job(id) from None
    svc.log.info("job.decided", ticket_id=id, verdict=body.verdict, by=by)
    return _job_row(state)


# --- stations ----------------------------------------------------------------------------------


def station_row(record: StationRecord, lease: Lease | None) -> dict[str, Any]:
    """One row of the wizard's station list: the record joined with its lease."""
    sentence = record.sentence()
    sentence += f" {lease.sentence()}" if lease is not None else " Free."
    return {
        "name": record.name,
        "description": record.description,
        "free": lease is None,
        "holder": f"{lease.ticket_id} ({lease.user})" if lease is not None else None,
        "enrolled": record.enrolled,
        "sentence": sentence,
    }


@router.get("/stations")
def stations(request: Request) -> list[dict[str, Any]]:
    svc = _services(request)
    now = svc.clock.now()
    return [
        station_row(record, svc.leases.holder(record.name, now)) for record in svc.registry.list()
    ]


def _record_row(record: StationRecord) -> dict[str, Any]:
    return {
        **record.model_dump(mode="json"),
        "enrolled": record.enrolled,
        "sentence": record.sentence(),
    }


def _manage(request: Request, verb: str) -> Any:
    svc = _services(request)
    require(identity_of(request), STATIONS_CAPABILITY, verb=verb)
    return svc


def _get_record(svc: Any, name: str) -> StationRecord:
    try:
        record: StationRecord = svc.registry.get(name)
    except StationError as exc:
        raise ServiceError(404, exc.message) from None
    return record


@router.get("/station-records")
def station_records(request: Request) -> list[dict[str, Any]]:
    svc = _manage(request, "see the station records")
    return [_record_row(record) for record in svc.registry.list()]


@router.post("/station-records")
def add_station_record(request: Request, body: NewStationBody) -> dict[str, Any]:
    svc = _manage(request, "add a station")
    try:
        svc.registry.get(body.name)
    except StationError:
        pass
    else:
        raise ServiceError(
            409,
            ThreePartMessage(
                f"There is already a station called {body.name}.",
                "Station names are unique on an installation.",
                "Pick another name, or issue a new code for the existing station.",
            ),
        )
    record = svc.registry.put(StationRecord(name=body.name, description=body.description))
    svc.log.info("station.added", station=body.name, by=identity_of(request).user)
    return _record_row(record)


@router.put("/station-records/{name}/tuning")
def tune_station_record(request: Request, name: str, body: TuningBody) -> dict[str, Any]:
    svc = _manage(request, f"tune {name}")
    record = _get_record(svc, name)
    update: dict[str, Any] = {}
    if body.screen is not None:
        update["screen"] = body.screen
    if body.retention is not None:
        update["retention"] = body.retention
    if body.vnc_enabled is not None:
        update["vnc"] = record.vnc.model_copy(update={"enabled": body.vnc_enabled})
    record = svc.registry.put(record.model_copy(update=update))
    svc.log.info("station.tuned", station=name, fields=sorted(update))
    return _record_row(record)


@router.post("/station-records/{name}/code")
def issue_station_code(request: Request, name: str) -> dict[str, Any]:
    svc = _manage(request, f"issue an enrolment code for {name}")
    try:
        issued = svc.enrolment.issue(name, by=identity_of(request).user)
    except StationError as exc:
        raise ServiceError(404, exc.message) from None
    svc.log.info("station.code_issued", station=name, expires_at=issued.expires_at.isoformat())
    return dict(issued.model_dump(mode="json"))


@router.post("/station-records/{name}/revoke")
def revoke_station_record(request: Request, name: str) -> dict[str, Any]:
    svc = _manage(request, f"revoke the enrolment of {name}")
    try:
        record = svc.enrolment.revoke(name)
    except StationError as exc:
        raise ServiceError(404, exc.message) from None
    svc.log.info("station.revoked", station=name, by=identity_of(request).user)
    return _record_row(record)


@router.delete("/station-records/{name}")
def delete_station_record(request: Request, name: str) -> dict[str, Any]:
    svc = _manage(request, f"remove {name}")
    _get_record(svc, name)
    svc.enrolment.revoke(name)
    svc.registry.remove(name)
    svc.log.info("station.removed", station=name, by=identity_of(request).user)
    return {"sentence": f"{name} was removed; its enrolment and batch key are revoked."}


# --- templates and the MES ---------------------------------------------------------------------


@router.get("/templates")
def templates(request: Request) -> list[dict[str, Any]]:
    svc = _services(request)
    ensure_shipped_templates(svc.templates_dir)
    return [
        {**template.model_dump(mode="json"), "sentence": template.sentence()}
        for template in templates_in(svc.templates_dir, svc.log)
    ]


@router.get("/mes/pending")
def mes_pending(request: Request) -> list[dict[str, Any]]:
    svc = _services(request)
    return [ticket.model_dump(mode="json") for ticket in pending_tickets(svc.mes)]
