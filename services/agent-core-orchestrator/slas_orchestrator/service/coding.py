"""`/v1/coding`: the New coding task wizard and the task list (contract §5, CLAUDE.md §9, §10.1).

    detect · propose · resolve        the wizard's three steps, nothing runs
    remotes · skills                  what the person may pick: saved remotes, enabled skills
    readiness                         is a healthy instance serving the coder role?
    tasks                             Start task → ticket → the kernel runs in a thread
                                      (refused with a sentence while the coder is not ready);
                                      DELETE removes a finished task and its files

The wire spells the export choice `export_target`; the `Breakdown` model calls it `export`.
The two adapters below translate, so the model stays as the agent tests know it.
"""

from __future__ import annotations

import shutil
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
from slas_schemas.ticket import TicketState

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


# --- readiness ----------------------------------------------------------------------------

CODER_ROLE: Final = "coder"


def coder_readiness(deps: Deps) -> tuple[bool, str, ThreePartMessage | None]:
    """Whether a healthy instance serves the coder role, as the gateway reports it.

    Returns (ready, sentence, problem): the sentence is what the wizard shows before Start;
    the problem is the three-part answer a refused start carries. A task started while the
    coder is still loading would fail minutes later at its first edit, so it is refused now.
    """
    status = deps.gateway_status()
    if status is None:
        problem = ThreePartMessage(
            "The LLM gateway did not answer, so the state of the coding model is unknown.",
            "The llm-gateway container is starting or stopped.",
            "Wait a moment and try again; if it repeats, run `slas logs llm-gateway` on the host.",
        )
        return False, f"{problem.what_happened} {problem.what_to_do}", problem
    rows = status.get("roles") if isinstance(status, dict) else None
    row = next((r for r in rows or [] if isinstance(r, dict) and r.get("role") == CODER_ROLE), None)
    instance = str(row.get("instance") or f"vllm-{CODER_ROLE}") if row else f"vllm-{CODER_ROLE}"
    model_id = str(row.get("model_id") or "") if row else ""
    if row is not None and row.get("healthy") is True:
        served = f"{instance} ({model_id})" if model_id else instance
        return True, f"The coding model is ready: {served} serves the coder role.", None
    problem = ThreePartMessage(
        "The coding model is not ready yet.",
        f"{instance} is still starting, or the model manager has not reported it healthy; "
        "the first start of a large model can take several minutes.",
        "Watch the Models page and start the task when the coder role shows healthy.",
    )
    return False, f"{problem.what_happened} {problem.likely_cause} {problem.what_to_do}", problem


@router.get("/readiness")
def readiness(request: Request) -> dict[str, Any]:
    """What the wizard says above Start task: is the coding model ready?"""
    identity_of(request)
    ready, sentence, _ = coder_readiness(deps_of(request))
    return {"ready": ready, "sentence": sentence}


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
    ready, _, not_ready = coder_readiness(deps)
    if not ready and not_ready is not None:
        raise ServiceError(503, not_ready)
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


#: A task may be removed once it has stopped: it is done, it failed, or it waits for review.
REMOVABLE_STATES: Final = frozenset(
    {TicketState.DONE, TicketState.FAILED, TicketState.NEEDS_REVIEW}
)


@router.delete("/tasks/{ticket_id}")
def remove_task(request: Request, ticket_id: str = PathParam()) -> dict[str, Any]:
    """Remove a finished task: its ticket and journal, SOP, artifacts, any open sandbox
    session, and the child tickets spawned from it. The project directory stays: it is the
    person's repository (CLAUDE.md §4.4)."""
    identity = identity_of(request)
    deps = deps_of(request)
    ticket = load_ticket(deps, identity, ticket_id)
    state = deps.registry.state_of(ticket.id)
    if (state is not None and state.running) or ticket.state not in REMOVABLE_STATES:
        raise ServiceError(
            409,
            ThreePartMessage(
                f"{ticket.id} is still running, so it cannot be removed.",
                f"Its state is {ticket.state.value}; only a task that is done, failed or "
                "waiting for review can be removed.",
                "Wait until the task stops, then remove it; the Coding page shows when.",
            ),
        )
    deps.registry.forget(ticket.id)
    session = deps.coding_executor.forget(ticket.id)
    if session is not None:
        try:
            deps.sandbox.close_session(session.id)
        except ServiceError as exc:
            # The sandbox manager reaps sessions on their TTL; a close that fails now is
            # not a reason to keep the ticket.
            deps.log.warning("coding.session_close_failed", ticket_id=ticket.id, why=str(exc))
    removed: list[str] = []
    children = [child for child in deps.tickets() if child.parent == ticket.id]
    for gone in (*children, ticket):
        for path in (
            deps.data_root / "Tickets" / gone.id,
            deps.data_root / "SOP" / gone.id,
            deps.data_root / "Coding" / gone.user / "Artifacts" / gone.id,
        ):
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path.relative_to(deps.data_root)))
    deps.log.info(
        "coding.task_removed", ticket_id=ticket.id, user=identity.user, removed=len(removed)
    )
    what = "and its files were" if removed else "was"
    tail = (
        f" {len(children)} child ticket{'s' if len(children) != 1 else ''} went with it."
        if children
        else ""
    )
    return {"sentence": f"{ticket.id} {what} removed.{tail} The project's repository stays."}
