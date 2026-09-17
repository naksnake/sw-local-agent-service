"""`/v1/factory`: the New factory job wizard, the job list and the line lead's verbs
(contract §5 Factory).

    mes-tickets        → [MesTicket]    production tickets waiting in the executor's MES drop
    labels/parse       → trigger|problem  a label scan or manual entry as a MesTicket
    stations           → [StationView]  the executor's stations with their leases
    templates          → [TemplateView] the loops this agent runs (what `choose_template` accepts)
    jobs               → JobView        ingest the trigger, bind the template, start the kernel
    jobs/{id}/decide   → JobView        the line lead's PASS/FAIL (factory:verdict) → `decide`
    jobs/{id}/control  → ControlView    pause/resume/abort the runner (factory:control) → `control`
    jobs, jobs/{id}                     ticket + the executor's test-step map

Same wiring as the Validation router: a `FactoryDeps` on `app.state.factory`, the run in
the `Runner`, the ticket id from the `WatchedTicketStore`. INV-7 and INV-11 are the
executor's: this router only carries the person's identity to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request

from slas_factory_executor.mes import MesAdapter
from slas_http.errors import ServiceError
from slas_http.identity import identity_of, require
from slas_kernel.kernel import Kernel
from slas_orchestrator.factory.agent import FactoryAgent, TriggerError
from slas_orchestrator.remote import ExecutorReader
from slas_orchestrator.service.models import (
    ControlRequest,
    ControlView,
    DecideRequest,
    JobView,
    LabelParse,
    ParseLabelRequest,
    StartJobRequest,
    StationView,
    TemplateView,
)
from slas_orchestrator.service.validation import (
    Runner,
    WatchedTicketStore,
    agent_tickets,
    load_ticket,
    start_and_wait,
)
from slas_orchestrator.service.views_factory import (
    control_view,
    job_view,
    rules_sentence,
    station_views,
    template_view,
)
from slas_orchestrator.service.views_validation import problem_sentence
from slas_schemas.errors import ThreePartMessage
from slas_schemas.job import MesTicket
from slas_schemas.ticket import Ticket

router = APIRouter(prefix="/v1/factory")


@dataclass
class FactoryDeps:
    """Everything the router needs; `kernel.agent` must be `agent` and `kernel.store` must be
    `store`. `mes`, when given, receives the verdict after each run (`report_verdict`)."""

    kernel: Kernel
    agent: FactoryAgent
    store: WatchedTicketStore
    executor_reader: ExecutorReader
    runner: Runner
    mes: MesAdapter | None = None
    start_timeout_s: float = 30.0


def deps_of(request: Request) -> FactoryDeps:
    deps = getattr(request.app.state, "factory", None)
    if not isinstance(deps, FactoryDeps):  # pragma: no cover — a wiring defect
        raise ServiceError(
            503,
            ThreePartMessage(
                "The Factory Agent is not wired into this orchestrator.",
                "app.state.factory is missing or is not a FactoryDeps.",
                "Run `slas logs agent-core-orchestrator` on the host and quote the trace id.",
            ),
        )
    return deps


def _job_ticket(deps: FactoryDeps, ticket_id: str) -> Ticket:
    return load_ticket(deps.store, ticket_id, agent="factory", noun="factory job", page="Factory")


def detail_view(deps: FactoryDeps, ticket: Ticket, *, rules_note: str | None = None) -> JobView:
    return job_view(ticket, deps.executor_reader.job_state(ticket.id), rules_note=rules_note)


# --- routes ------------------------------------------------------------------------------------


@router.get("/mes-tickets")
def mes_tickets(request: Request) -> list[MesTicket]:
    identity_of(request)
    return deps_of(request).executor_reader.mes_pending()


@router.post("/labels/parse", response_model_exclude_none=True)
def parse_label(request: Request, body: ParseLabelRequest) -> LabelParse:
    identity = identity_of(request)
    deps = deps_of(request)
    try:
        return LabelParse(trigger=deps.agent.parse_label(body.text, by=identity.user))
    except TriggerError as exc:
        return LabelParse(problem=problem_sentence(exc.message))


@router.get("/stations")
def stations(request: Request) -> list[StationView]:
    identity_of(request)
    return station_views(deps_of(request).executor_reader.stations())


@router.get("/templates")
def templates(request: Request) -> list[TemplateView]:
    identity_of(request)
    deps = deps_of(request)
    return [
        template_view(template)
        for _, template in sorted(deps.agent.templates.items())
        if not template.id.endswith("-no-backup")
    ]


@router.post("/jobs")
def start_job(request: Request, body: StartJobRequest) -> JobView:
    identity_of(request)
    deps = deps_of(request)
    trigger = body.trigger
    job = deps.agent.ingest(trigger)
    try:
        template = deps.agent.choose_template(
            job, body.template_id, backup_station=body.rules.backup_station
        )
    except TriggerError as exc:
        raise ServiceError(400, exc.message) from exc
    note = rules_sentence(body.rules, template)

    def run() -> Ticket:
        ticket = deps.kernel.run(trigger)
        if deps.mes is not None:
            state = deps.executor_reader.job_state(ticket.id)
            if state is not None:
                deps.agent.report_verdict(ticket, state, deps.mes)
        return ticket

    ticket_id = start_and_wait(
        store=deps.store,
        runner=deps.runner,
        job=job,
        fn=run,
        timeout_s=deps.start_timeout_s,
        noun="factory job",
    )
    return detail_view(deps, deps.store.load(ticket_id), rules_note=note)


@router.post("/jobs/{ticket_id}/decide")
def decide(request: Request, ticket_id: str, body: DecideRequest) -> JobView:
    identity = identity_of(request)
    require(identity, "factory:verdict", verb="decide PASS or FAIL for a unit")
    deps = deps_of(request)
    ticket = _job_ticket(deps, ticket_id)
    state = deps.executor_reader.decide(
        ticket.id, verdict=body.verdict, by=identity.name, note=body.note, identity=identity
    )
    if deps.mes is not None:
        deps.agent.report_verdict(ticket, state, deps.mes)
    return job_view(deps.store.load(ticket.id), state)


@router.post("/jobs/{ticket_id}/control")
def control(request: Request, ticket_id: str, body: ControlRequest) -> ControlView:
    identity = identity_of(request)
    require(identity, "factory:control", verb="pause, resume or abort a station")
    deps = deps_of(request)
    ticket = _job_ticket(deps, ticket_id)
    answer = deps.executor_reader.control(ticket.id, body.verb, by=identity.name, identity=identity)
    return control_view(answer)


@router.get("/jobs")
def list_jobs(request: Request) -> list[JobView]:
    identity_of(request)
    deps = deps_of(request)
    tickets = agent_tickets(deps.store, "factory")
    states = deps.executor_reader.list_jobs() if tickets else {}
    return [job_view(ticket, states.get(ticket.id)) for ticket in tickets]


@router.get("/jobs/{ticket_id}")
def get_job(request: Request, ticket_id: str) -> JobView:
    identity_of(request)
    deps = deps_of(request)
    return detail_view(deps, _job_ticket(deps, ticket_id))
