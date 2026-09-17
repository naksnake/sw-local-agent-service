"""`/v1/validation`: the New validation run wizard and the Runs list (contract §5 Validation).

    suites/parse  → SuiteView      the suite as items, destructive ones flagged
    targets       → [TargetView]   the executor's targets with their leases
    preview       → PlanPreview    compile, guardrails, the plan-approval cross-check; runs nothing
    runs          → RunView        ingest, choose the target, start the kernel in a thread
    runs/{id}/approve              every pending step approved by the acting person, then resume
    runs, runs/{id}                ticket + the executor's cycle map + console tail

The router imports nothing from the service skeleton: the integrator builds a
`ValidationDeps` and stores it on `app.state.validation`. The kernel run happens in whatever
the `Runner` does with it (a thread in the service, the calling thread in tests); the route
answers as soon as the kernel has saved the ticket, which `WatchedTicketStore` reports.
"""

from __future__ import annotations

import base64
import binascii
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, Request

from slas_http.errors import ServiceError
from slas_http.identity import Identity, identity_of, require
from slas_kernel.kernel import ApprovalPendingError, Kernel
from slas_kernel.store import TicketNotFoundError, TicketStore
from slas_orchestrator.remote import ExecutorReader, RunStateAnswer
from slas_orchestrator.service.models import (
    ParseSuiteRequest,
    PlanPreview,
    PreviewRequest,
    RunView,
    StartRunRequest,
    SuiteView,
    TargetView,
)
from slas_orchestrator.service.views_validation import (
    plan_preview,
    run_view,
    suite_from_view,
    suite_problem,
    suite_to_markdown,
    suite_view,
    target_views,
)
from slas_orchestrator.validation.agent import ValidationAgent
from slas_orchestrator.validation.compiler import PlanCompileError, compile_suite
from slas_orchestrator.validation.suite import SuiteError, parse_suite_md, parse_suite_xlsx
from slas_schemas.common import AgentName
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import Job, Upload
from slas_schemas.plan import Plan
from slas_schemas.ticket import Ticket
from slas_schemas.vote import ConsensusVerdict

router = APIRouter(prefix="/v1/validation")

# --- shared run plumbing (used by the Factory router too) --------------------------------------


class Runner(Protocol):
    """Where a kernel run happens. `key` is the job id (the ticket id does not exist until the
    kernel creates it); `fn` performs the whole run and returns the finished ticket. The
    service's RunRegistry starts a thread; tests call `fn()` right away."""

    def start(self, key: str, fn: Callable[[], Ticket]) -> None: ...


class TicketWaiter:
    """Resolves with the ticket id the kernel allocates for one job."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.ticket_id: str | None = None
        self._event = threading.Event()

    def resolve(self, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        self._event.set()

    def wait(self, timeout_s: float) -> str | None:
        self._event.wait(timeout_s)
        return self.ticket_id


class WatchedTicketStore:
    """A `TicketStore` that tells a waiting route which ticket the kernel created for its job,
    so the route can answer with the ticket id while the run goes on in its thread."""

    def __init__(self, inner: TicketStore) -> None:
        self.inner = inner
        self._waiters: list[TicketWaiter] = []
        self._lock = threading.Lock()

    def expect(self, job_id: str) -> TicketWaiter:
        waiter = TicketWaiter(job_id)
        with self._lock:
            self._waiters.append(waiter)
        return waiter

    def next_ticket_id(self, agent: AgentName) -> str:
        return self.inner.next_ticket_id(agent)

    def save(self, ticket: Ticket) -> None:
        self.inner.save(ticket)
        if ticket.parent is not None:
            return
        with self._lock:
            for waiter in list(self._waiters):
                if waiter.job_id == ticket.job.id and waiter.ticket_id is None:
                    waiter.resolve(ticket.id)
                    self._waiters.remove(waiter)

    def load(self, ticket_id: str) -> Ticket:
        return self.inner.load(ticket_id)

    def list_ids(self) -> list[str]:
        return self.inner.list_ids()


def start_and_wait(
    *,
    store: WatchedTicketStore,
    runner: Runner,
    job: Job,
    fn: Callable[[], Ticket],
    timeout_s: float,
    noun: str,
) -> str:
    """Start the run and return the ticket id the kernel allocated, or a three-part 503."""
    waiter = store.expect(job.id)
    runner.start(job.id, fn)
    ticket_id = waiter.wait(timeout_s)
    if ticket_id is None:
        raise ServiceError(
            503,
            ThreePartMessage(
                f"The {noun} did not start within {timeout_s:g} s.",
                "The orchestrator is busy, or the kernel could not create the ticket.",
                "Try again; if it repeats, run `slas logs agent-core-orchestrator` on the host.",
            ),
        )
    return ticket_id


def load_ticket(
    store: TicketStore, ticket_id: str, *, agent: AgentName, noun: str, page: str
) -> Ticket:
    try:
        ticket = store.load(ticket_id)
    except TicketNotFoundError:
        ticket = None
    if ticket is None or ticket.agent != agent or ticket.parent is not None:
        raise ServiceError(
            404,
            ThreePartMessage(
                f"There is no {noun} {ticket_id}.",
                "The link is stale, or the ticket belongs to another agent.",
                f"Go back to the {page} page.",
            ),
        )
    return ticket


def agent_tickets(store: TicketStore, agent: AgentName) -> list[Ticket]:
    """The agent's run tickets (not the bug tickets spawned from findings), newest first."""
    tickets: list[Ticket] = []
    for ticket_id in store.list_ids():
        if not ticket_id.startswith(f"T-{agent}-"):
            continue
        try:
            ticket = store.load(ticket_id)
        except TicketNotFoundError:  # pragma: no cover — removed between list and load
            continue
        if ticket.parent is None:
            tickets.append(ticket)
    return sorted(tickets, key=lambda t: (t.created_at, t.id), reverse=True)


def preview_cross_check(plan: Plan, kernel: Kernel) -> ConsensusVerdict | None:
    """The plan-approval cross-check exactly as the kernel runs it before ACT (§5.3), without
    starting anything. INV-11: the verdict is shown to the person; it approves nothing."""
    if kernel.plan_checker is None:
        return None
    evidence = [plan.summary, *(f"{s.n}. {s.title} [{s.risk}]" for s in plan.steps)]
    return kernel.plan_checker.cross_check("plan_approval", evidence)


# --- the dependencies ------------------------------------------------------------------------


@dataclass
class ValidationDeps:
    """Everything the router needs; `kernel.agent` must be `agent` and `kernel.store` must be
    `store`, so the run the route starts is the one the views read."""

    kernel: Kernel
    agent: ValidationAgent
    store: WatchedTicketStore
    executor_reader: ExecutorReader
    runner: Runner
    start_timeout_s: float = 30.0


def deps_of(request: Request) -> ValidationDeps:
    deps = getattr(request.app.state, "validation", None)
    if not isinstance(deps, ValidationDeps):  # pragma: no cover — a wiring defect
        raise ServiceError(
            503,
            ThreePartMessage(
                "The Validation Agent is not wired into this orchestrator.",
                "app.state.validation is missing or is not a ValidationDeps.",
                "Run `slas logs agent-core-orchestrator` on the host and quote the trace id.",
            ),
        )
    return deps


# --- helpers -----------------------------------------------------------------------------------


def _decode_workbook(filename: str, content_base64: str) -> bytes:
    try:
        return base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ServiceError(
            400,
            ThreePartMessage(
                f"{filename} could not be decoded.",
                "The workbook did not arrive as valid base64.",
                "Upload the workbook again.",
            ),
        ) from exc


def parse_suite_request(body: ParseSuiteRequest) -> SuiteView:
    filename = body.filename
    try:
        if filename.lower().endswith(".xlsx"):
            if body.content_base64 is None:
                raise SuiteError(
                    ThreePartMessage(
                        f"{filename} arrived without its file.",
                        "The wizard sends an .xlsx workbook as content_base64.",
                        "Upload the workbook again.",
                    )
                )
            raw = _decode_workbook(filename, body.content_base64)
            with tempfile.TemporaryDirectory(prefix="slas-suite-") as folder:
                path = Path(folder) / Path(filename).name
                path.write_bytes(raw)
                return suite_view(parse_suite_xlsx(path))
        if body.text is None:
            raise SuiteError(
                ThreePartMessage(
                    f"{filename} arrived without its content.",
                    "The wizard sends the suite text with the upload.",
                    "Upload the suite again.",
                )
            )
        return suite_view(parse_suite_md(body.text, source=filename))
    except SuiteError as exc:
        return suite_problem(filename, exc.message)


def _no_items(view: SuiteView) -> ServiceError:
    return ServiceError(
        400,
        ThreePartMessage(
            f"{view.source} has no items to run.",
            view.problem or "The suite was not parsed, or every row was empty.",
            "Fix the suite and parse it again.",
        ),
    )


def compile_view(deps: ValidationDeps, view: SuiteView, target: str, *, job_id: str) -> Plan:
    if not view.items:
        raise _no_items(view)
    try:
        return compile_suite(
            suite_from_view(view),
            job_id=job_id,
            target=target,
            guardrails=deps.agent.guardrails,
            now=deps.kernel.clock.now(),
            compiler=deps.agent.compiler,
        )
    except PlanCompileError as exc:
        raise ServiceError(400, exc.message) from exc


def _state_of(deps: ValidationDeps, ticket_id: str) -> RunStateAnswer | None:
    return deps.executor_reader.run_state(ticket_id)


def detail_view(deps: ValidationDeps, ticket: Ticket) -> RunView:
    answer = _state_of(deps, ticket.id)
    return run_view(
        ticket,
        answer.state if answer else None,
        answer.console_tail if answer else [],
    )


# --- routes ------------------------------------------------------------------------------------


@router.post("/suites/parse")
def parse_suite(request: Request, body: ParseSuiteRequest) -> SuiteView:
    identity_of(request)
    return parse_suite_request(body)


@router.get("/targets")
def targets(request: Request) -> list[TargetView]:
    identity_of(request)
    return target_views(deps_of(request).executor_reader.targets())


@router.post("/preview")
def preview(request: Request, body: PreviewRequest) -> PlanPreview:
    identity_of(request)
    deps = deps_of(request)
    plan = compile_view(deps, body.suite, body.target, job_id="preview")
    return plan_preview(plan, deps.agent.guardrails, preview_cross_check(plan, deps.kernel))


@router.post("/runs")
def start_run(request: Request, body: StartRunRequest) -> RunView:
    identity = identity_of(request)
    deps = deps_of(request)
    if not body.suite.items:
        raise _no_items(body.suite)
    text = suite_to_markdown(body.suite)
    source = body.suite.source
    filename = source if source.lower().endswith(".md") else f"{Path(source).stem or 'suite'}.md"
    upload = Upload(
        filename=filename,
        uploaded_by=identity.user,
        content_type="text/markdown",
        content=text,
        size_bytes=len(text.encode("utf-8")),
    )
    try:
        job = deps.agent.ingest(upload)
    except SuiteError as exc:
        raise ServiceError(400, exc.message) from exc
    deps.agent.choose_target(job, body.target)
    # Compile once here so a suite the compiler refuses is a 400 now, not a ticket left Open.
    compile_view(deps, suite_view(deps.agent.suite(job.id)), body.target, job_id=job.id)
    ticket_id = start_and_wait(
        store=deps.store,
        runner=deps.runner,
        job=job,
        fn=lambda: deps.kernel.run(upload),
        timeout_s=deps.start_timeout_s,
        noun="validation run",
    )
    return detail_view(deps, deps.store.load(ticket_id))


def _approve_all(deps: ValidationDeps, ticket: Ticket, identity: Identity) -> Ticket:
    for approval in ticket.pending_approvals:
        ticket = deps.kernel.approve(ticket.id, approval.step_id, identity.name)
    return ticket


@router.post("/runs/{ticket_id}/approve")
def approve_run(request: Request, ticket_id: str) -> RunView:
    identity = identity_of(request)
    require(identity, "approve:destructive", verb="approve destructive steps")
    deps = deps_of(request)
    ticket = load_ticket(
        deps.store, ticket_id, agent="validation", noun="validation run", page="Validation"
    )
    if not ticket.pending_approvals:
        raise ServiceError(
            409,
            ThreePartMessage(
                f"{ticket.id} has nothing waiting for approval.",
                f"It is {ticket.state.value.lower()}; every destructive step was already decided.",
                "Open the run to see where it is.",
            ),
        )
    ticket = _approve_all(deps, ticket, identity)

    def resume() -> Ticket:
        try:
            return deps.kernel.resume(ticket.id)
        except ApprovalPendingError as exc:  # pragma: no cover — every step was just approved
            return exc.ticket

    deps.runner.start(ticket.job.id, resume)
    return detail_view(deps, deps.store.load(ticket.id))


@router.get("/runs")
def list_runs(request: Request) -> list[RunView]:
    identity_of(request)
    deps = deps_of(request)
    tickets = agent_tickets(deps.store, "validation")
    states = deps.executor_reader.list_runs() if tickets else {}
    return [run_view(ticket, states.get(ticket.id), []) for ticket in tickets]


@router.get("/runs/{ticket_id}")
def get_run(request: Request, ticket_id: str) -> RunView:
    identity_of(request)
    deps = deps_of(request)
    ticket = load_ticket(
        deps.store, ticket_id, agent="validation", noun="validation run", page="Validation"
    )
    return detail_view(deps, ticket)
