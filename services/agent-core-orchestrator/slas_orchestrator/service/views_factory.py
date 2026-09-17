"""The Factory page's views, built from a ticket and the executor's job state (§5 JobView).

The ticket is the kernel's truth (state, votes, findings, exports, the SOP); the test-step
map with its screenshots is the executor's; the verdict and the hold come from the same
`JobState` the line lead decides on.
"""

from __future__ import annotations

from typing import Any

from slas_factory_executor.executor import JobState
from slas_factory_executor.templates import TestLoopTemplate
from slas_orchestrator.remote import ControlAnswer
from slas_orchestrator.service.models import (
    ControlView,
    JobRules,
    JobView,
    StationView,
    StepCellView,
    TemplateView,
)
from slas_orchestrator.service.views_validation import vote_sentences
from slas_schemas.plan import Plan
from slas_schemas.ticket import Ticket, TicketState

FIXED_RULES_SENTENCE = (
    "PASS needs 3 of 3 voters; anything else holds the station for the line lead. "
    "The SOP is exported in English and Chinese."
)


def rules_sentence(rules: JobRules, template: TestLoopTemplate) -> str:
    """What the wizard's third step will do — and which of its knobs are fixed today."""
    backed_up = any(s.primitive == "backup_station" for s in template.steps)
    parts = [FIXED_RULES_SENTENCE]
    parts.append(
        "The station state is backed up." if backed_up else "The station state is not backed up."
    )
    fixed_changed = (
        rules.voters != 3 or rules.on_fail not in ("hold", "hold_station") or not rules.export_sop
    )
    if fixed_changed:
        parts.append(
            "Voters, on-fail behaviour and SOP export are fixed on this installation; the other "
            "values you sent were not applied."
        )
    return " ".join(parts)


def plan_rules_sentence(plan: Plan | None) -> str:
    if plan is None:
        return FIXED_RULES_SENTENCE
    backed_up = any(s.primitive == "backup_station" for s in plan.steps)
    return FIXED_RULES_SENTENCE + (
        " The station state is backed up." if backed_up else " The station state is not backed up."
    )


def template_view(template: TestLoopTemplate) -> TemplateView:
    return TemplateView(
        id=template.id,
        name=template.name,
        description=template.description,
        steps=[s.title for s in template.steps],
        skills=list(template.skills),
        sentence=template.sentence(),
    )


def station_views(rows: list[dict[str, Any]]) -> list[StationView]:
    views: list[StationView] = []
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        holder = row.get("holder")
        views.append(
            StationView(
                name=name,
                description=str(row.get("description") or ""),
                free=bool(row.get("free", holder is None)),
                holder=str(holder) if holder else None,
                enrolled=bool(row.get("enrolled", False)),
                sentence=str(row.get("sentence") or f"{name} is {'busy' if holder else 'free'}."),
            )
        )
    return views


def control_view(answer: ControlAnswer) -> ControlView:
    control = answer.result.control
    return ControlView(
        sentence=answer.result.sentence,
        watch_url=answer.watch_url,
        watch_problem=answer.watch_problem,
        station=control.station if control else "",
        paused=control.paused if control else False,
        aborted=control.aborted if control else False,
        by=control.by if control else "",
    )


def _lease_arg(plan: Plan | None, name: str) -> str:
    if plan is None:
        return ""
    for step in plan.steps:
        if step.primitive == "lease_station":
            return str(step.args.get(name, ""))
    return ""


def waiting_cells(plan: Plan | None) -> list[StepCellView]:
    if plan is None:
        return []
    return [StepCellView(n=s.n, title=s.title, status="waiting") for s in plan.steps]


def job_sentence(ticket: Ticket, state: JobState | None) -> str:
    if (
        state is not None
        and state.cells
        and ticket.state
        not in (
            TicketState.OPEN,
            TicketState.PLANNED,
            TicketState.APPROVED,
        )
    ):
        if ticket.state is TicketState.RUNNING:
            return f"{ticket.sentence()} {state.sentence()}"
        return state.sentence()
    return ticket.sentence()


def job_view(ticket: Ticket, state: JobState | None, *, rules_note: str | None = None) -> JobView:
    plan = ticket.plan
    cells = (
        [
            StepCellView(
                n=c.n,
                title=c.title,
                status=c.status,
                sentence=c.sentence,
                screenshot=c.screenshots[-1] if c.screenshots else None,
                screenshots=list(c.screenshots),
            )
            for c in state.cells
        ]
        if state is not None and state.cells
        else waiting_cells(plan)
    )
    station = ticket.job.target.ref if ticket.job.target else (state.station if state else "")
    backups = [e.path for e in ticket.exports if e.kind == "backup"]
    draft = next((f.ticket_id for f in ticket.findings if f.ticket_id), None)
    return JobView(
        ticket_id=ticket.id,
        title=ticket.title,
        station=station,
        unit_sn=(state.unit_sn if state and state.unit_sn else _lease_arg(plan, "unit_sn")),
        state=ticket.state.value,
        sentence=job_sentence(ticket, state),
        cells=cells,
        verdict=state.verdict if state else None,
        votes=vote_sentences(ticket.votes),
        held=state.held if state else False,
        mes_ticket_no=(
            state.mes_ticket_no
            if state and state.mes_ticket_no
            else _lease_arg(plan, "mes_ticket_no")
        ),
        verdict_sentence=state.verdict_sentence if state else "",
        decided_by=state.decided_by if state else "",
        draft_ticket_id=draft,
        backup_path=backups[0] if backups else None,
        rules_sentence=rules_note if rules_note is not None else plan_rules_sentence(plan),
        created_at=ticket.created_at.isoformat(),
    )
