"""`/v1/coding`: the New coding task wizard and the task list (contract §5, CLAUDE.md §9, §10.1).

    detect · propose · resolve        the wizard's three steps, nothing runs
    remotes · skills                  what the person may pick: saved remotes, enabled skills
    tasks                             Start task → ticket → the kernel runs in a thread

The wire spells the export choice `export_target`; the `Breakdown` model calls it `export`.
The two adapters below translate, so the model stays as the agent tests know it.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from slas_http.errors import ServiceError
from slas_http.identity import Identity, identity_of
from slas_orchestrator.coding.breakdown import Breakdown
from slas_orchestrator.coding.plan_doc import MAX_PLAN_BYTES, PlanError
from slas_orchestrator.service.deps import (
    Deps,
    deps_of,
    load_ticket,
    may_see,
    problem,
    workspace_user,
)
from slas_orchestrator.service.runs import RunStartError
from slas_orchestrator.service.skills import enabled_for, load_library
from slas_orchestrator.service.views import coding_task_view
from slas_sandbox_manager.toolchains import ToolchainError
from slas_schemas.common import validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import Upload

router = APIRouter(prefix="/v1/coding")

AGENT: Final = "coding"
EXPORT_WIRE_KEY: Final = "export_target"


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=MAX_PLAN_BYTES)


class ProposeBody(PlanBody):
    filename: str = Field(default="plan.md", min_length=1, max_length=200)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: list[dict[str, Any]]


class TaskBody(ProposeBody):
    breakdown: dict[str, Any]


# --- wire adapters ------------------------------------------------------------------------


def breakdown_to_wire(breakdown: Breakdown) -> dict[str, Any]:
    data = breakdown.model_dump(mode="json")
    data[EXPORT_WIRE_KEY] = data.pop("export")
    return data


def breakdown_from_wire(data: dict[str, Any]) -> Breakdown:
    body = dict(data)
    if EXPORT_WIRE_KEY in body:
        body["export"] = body.pop(EXPORT_WIRE_KEY)
    try:
        return Breakdown.model_validate(body)
    except ValidationError as exc:
        raise ServiceError(
            400,
            ThreePartMessage(
                f"The breakdown could not be used: {validation_sentence(exc)}.",
                "The wizard sent a breakdown the Coding Agent does not accept.",
                "Go back to the review step, fix the highlighted field and press Start task.",
            ),
        ) from exc


def _plan_problem(exc: PlanError) -> ServiceError:
    return ServiceError(400, exc.message)


def _toolchain_problem(exc: ToolchainError) -> ServiceError:
    return ServiceError(400, exc.message)


# --- the wizard ---------------------------------------------------------------------------


@router.post("/languages/detect")
def detect_languages(request: Request, body: PlanBody) -> Any:
    identity_of(request)
    return deps_of(request).sandbox.post("/v1/languages/detect", {"plan": body.plan})


@router.post("/propose")
def propose(request: Request, body: ProposeBody) -> dict[str, Any]:
    identity = identity_of(request)
    deps = deps_of(request)
    upload = Upload(filename=body.filename, uploaded_by=workspace_user(identity), content=body.plan)
    try:
        job = deps.coding_agent.ingest(upload)
        return breakdown_to_wire(deps.coding_agent.propose(job))
    except PlanError as exc:
        raise _plan_problem(exc) from exc


@router.post("/toolchains/resolve")
def resolve_toolchains(request: Request, body: ResolveBody) -> Any:
    identity_of(request)
    return deps_of(request).sandbox.post("/v1/toolchains/resolve", {"choices": body.choices})


@router.get("/remotes")
def remotes(request: Request) -> dict[str, Any]:
    """The names of the person's saved remotes; an unreachable broker is a warning, not an error."""
    identity = identity_of(request)
    deps = deps_of(request)
    if deps.broker is None:
        return {"remotes": [], "warning": "No Git broker is configured on this installation."}
    try:
        rows = deps.broker.get("/v1/remotes", identity=identity)
    except ServiceError as exc:
        deps.log.warning("remotes.unavailable", why=exc.message.what_happened)
        return {
            "remotes": [],
            "warning": (
                f"{exc.message.what_happened} Saved remotes are not offered until it "
                "answers; you can still export a ZIP or a bundle."
            ),
        }
    names = [
        str(row["name"])
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("name")
    ]
    return {"remotes": names}


@router.get("/skills")
def coding_skills(request: Request) -> dict[str, Any]:
    identity_of(request)
    deps = deps_of(request)
    library = load_library(deps.settings.skills_library)
    enabled = enabled_for(library, deps.skill_state, AGENT)
    return {"skills": [{"id": s.id, "name": s.name} for s in enabled]}


# --- tasks --------------------------------------------------------------------------------


def _view(deps: Deps, ticket_id: str, identity: Identity) -> dict[str, Any]:
    ticket = load_ticket(deps, identity, ticket_id)
    return coding_task_view(
        ticket, ticket.plan, deps.journal_for(ticket.id), deps.registry.state_of(ticket.id)
    )


@router.post("/tasks")
def start_task(request: Request, body: TaskBody) -> dict[str, Any]:
    """Start task: record the approved breakdown, create the ticket, run the kernel."""
    identity = identity_of(request)
    deps = deps_of(request)
    user = workspace_user(identity)
    breakdown = breakdown_from_wire(body.breakdown)
    upload = Upload(filename=body.filename, uploaded_by=user, content=body.plan)
    try:
        job = deps.coding_agent.ingest(upload)
        deps.coding_agent.approve(job.id, breakdown)
    except PlanError as exc:
        raise _plan_problem(exc) from exc
    except ToolchainError as exc:
        raise _toolchain_problem(exc) from exc
    deps.coding_executor.display_names[user] = identity.name
    kernel = deps.kernel_factory(deps)
    try:
        state = deps.registry.start(job.id, lambda: kernel.run(upload))
    except RunStartError as exc:
        raise problem(exc.status, exc, fallback=exc.message) from exc
    deps.log.info("coding.task_started", ticket_id=state.ticket_id, job_id=job.id, user=user)
    assert state.ticket_id is not None  # noqa: S101 — start() returns only with a ticket
    return _view(deps, state.ticket_id, identity)


@router.get("/tasks")
def list_tasks(request: Request) -> list[dict[str, Any]]:
    identity = identity_of(request)
    deps = deps_of(request)
    return [
        coding_task_view(
            ticket, ticket.plan, deps.journal_for(ticket.id), deps.registry.state_of(ticket.id)
        )
        for ticket in deps.tickets()
        if ticket.agent == AGENT and ticket.parent is None and may_see(identity, ticket)
    ]


@router.get("/tasks/{ticket_id}")
def get_task(request: Request, ticket_id: str = PathParam()) -> dict[str, Any]:
    identity = identity_of(request)
    return _view(deps_of(request), ticket_id, identity)
