"""The validation executor's routes (docs/api-contract-round-2.md §6).

    POST /v1/execute                      one step for the kernel → Observation
    GET  /v1/runs · /v1/runs/{id}         RunState rows; one run with its console tail
    GET  /v1/targets                      the registry joined with the lease table
    POST /v1/targets/{alias}/arm|disarm   the arming gate; capability approve:destructive

Every refusal is three parts: an unknown primitive is a 400, a lease or guardrail problem a
409, an unknown target a 404, a BMC or credential problem a 502.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from slas_hal.credentials import CredentialError
from slas_hal.redfish import HalError
from slas_hal.targets import ArmingError, TargetError, TargetRecord
from slas_http.errors import ServiceError
from slas_http.identity import identity_of, require
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.leases import Lease, LeaseError
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Step
from slas_validation_executor.executor import RunState
from slas_validation_executor.guardrails import GuardrailError

CONSOLE_TAIL_LINES: Final = 40
ARM_CAPABILITY: Final = "approve:destructive"

router = APIRouter(prefix="/v1")


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: Step
    context: ExecutionContext


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = ""


def _services(request: Request) -> Any:
    return request.app.state.services


# --- errors ------------------------------------------------------------------------------------


def problem_for(exc: Exception, step: Step) -> ServiceError | None:
    """The three-part answer for an exception the executor let through, or None for a bug."""
    if isinstance(exc, UnknownPrimitiveError):
        return ServiceError(
            400,
            ThreePartMessage(
                str(exc),
                "The plan names a verb outside plans/primitives/validation.yaml.",
                "Recompile the plan; the compiler rejects unknown primitives.",
            ),
        )
    if isinstance(exc, LeaseError | GuardrailError | ArmingError):
        return ServiceError(409, exc.message)
    if isinstance(exc, TargetError):
        return ServiceError(404, exc.message)
    if isinstance(exc, HalError | CredentialError):
        return ServiceError(502, exc.message)
    if type(exc) is RuntimeError:
        # The executor's own order checks ("no baseline; run baseline_snapshot first").
        return ServiceError(
            409,
            ThreePartMessage(
                f"Step {step.id} ({step.title}) cannot run yet: {exc}.",
                "An earlier step of the plan did not finish, or the steps are out of order.",
                "Look at the run's earlier steps; the kernel resumes from the last one done.",
            ),
        )
    return None


# --- execute -----------------------------------------------------------------------------------


@router.post("/execute")
def execute(request: Request, body: ExecuteRequest) -> dict[str, Any]:
    svc = _services(request)
    with svc.locks.hold(body.context.ticket_id):
        try:
            observation = svc.executor.execute(body.step, body.context)
        except Exception as exc:
            problem = problem_for(exc, body.step)
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


# --- runs ---------------------------------------------------------------------------------------


def console_tail(run_dir: Path, lines: int = CONSOLE_TAIL_LINES) -> list[str]:
    path = run_dir / "console.log"
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()[-lines:]


def _run_ids(runs_dir: Path) -> list[str]:
    """Newest first, by the state file's modification time."""
    states = sorted(
        runs_dir.glob("*/cycles.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True
    )
    return [path.parent.name for path in states]


def _run_row(state: RunState) -> dict[str, Any]:
    return {
        "ticket_id": state.ticket_id,
        "state": state.model_dump(mode="json"),
        "sentence": state.sentence(),
    }


@router.get("/runs")
def runs(request: Request) -> list[dict[str, Any]]:
    svc = _services(request)
    runs_dir = svc.executor.data_root / "Validation" / "Runs"
    rows: list[dict[str, Any]] = []
    for ticket_id in _run_ids(runs_dir):
        state = svc.executor.state_for(ticket_id)
        if state is not None:
            rows.append(_run_row(state))
    return rows


def _no_such_run(ticket_id: str) -> ServiceError:
    return ServiceError(
        404,
        ThreePartMessage(
            f"There is no validation run for {ticket_id}.",
            "No cycle has started for that ticket on this executor, or the id is mistyped.",
            "Open the run from the Validation page; it appears once its first cycle starts.",
        ),
    )


@router.get("/runs/{id}")
def run(request: Request, id: str) -> dict[str, Any]:
    svc = _services(request)
    state = svc.executor.state_for(id)
    if state is None:
        raise _no_such_run(id)
    return {
        **state.model_dump(mode="json"),
        "sentence": state.sentence(),
        "console_tail": console_tail(svc.executor.run_dir(id)),
    }


# --- targets -----------------------------------------------------------------------------------


def target_row(record: TargetRecord, lease: Lease | None) -> dict[str, Any]:
    """One row of the wizard's target list: the record joined with its lease."""
    sentence = record.sentence()
    sentence += f" {lease.sentence()}" if lease is not None else " Free."
    return {
        "ref": record.alias,
        # The registry knows the vendor hint, not the model: reading the model needs the BMC.
        "model": record.vendor_hint or record.kind,
        "free": lease is None,
        "holder": f"{lease.ticket_id} ({lease.user})" if lease is not None else None,
        "armed": record.power_actions_enabled,
        "sentence": sentence,
    }


@router.get("/targets")
def targets(request: Request) -> list[dict[str, Any]]:
    svc = _services(request)
    now = svc.clock.now()
    return [
        target_row(record, svc.leases.holder(record.alias, now)) for record in svc.registry.list()
    ]


def _arming(request: Request, alias: str, body: NoteBody, *, arm: bool) -> dict[str, Any]:
    svc = _services(request)
    identity = identity_of(request)
    verb = f"{'arm' if arm else 'disarm'} power actions on {alias}"
    require(identity, ARM_CAPABILITY, verb=verb)
    now = svc.clock.now()
    try:
        record = (
            svc.registry.arm(alias, by=identity.name, at=now, note=body.note)
            if arm
            else svc.registry.disarm(alias)
        )
    except TargetError as exc:
        raise ServiceError(404, exc.message) from None
    svc.log.info("target.armed" if arm else "target.disarmed", alias=alias, by=identity.user)
    return target_row(record, svc.leases.holder(alias, now))


@router.post("/targets/{alias}/arm")
def arm(request: Request, alias: str, body: NoteBody) -> dict[str, Any]:
    return _arming(request, alias, body, arm=True)


@router.post("/targets/{alias}/disarm")
def disarm(request: Request, alias: str, body: NoteBody) -> dict[str, Any]:
    return _arming(request, alias, body, arm=False)
