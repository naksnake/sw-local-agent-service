"""The Validation page's views, built from a ticket and the executor's run state (§5 RunView).

Code joins, never a model: the ticket is the kernel's truth, the LED cycle map is the
executor's, and the console tail is the last lines of the run's `console.log`.
"""

from __future__ import annotations

from typing import Any

from slas_hal.primitives import approval_kind
from slas_orchestrator.service.models import (
    CycleCellView,
    FindingRef,
    PlanPreview,
    PlanStepView,
    RunView,
    SuiteItemView,
    SuiteView,
    TargetView,
)
from slas_orchestrator.validation.compiler import map_action
from slas_orchestrator.validation.suite import Suite, SuiteItem
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Plan
from slas_schemas.ticket import Ticket, TicketState
from slas_schemas.vote import Vote
from slas_validation_executor.executor import RunState
from slas_validation_executor.guardrails import Guardrails

_VOTE_VERB = {"approve": "approves", "concern": "has a concern", "reject": "rejects"}


def problem_sentence(message: ThreePartMessage) -> str:
    return f"{message.what_happened} {message.likely_cause} {message.what_to_do}"


def vote_sentences(votes: list[Vote]) -> list[str]:
    return [f"{v.voter} {_VOTE_VERB.get(v.verdict, v.verdict)}: {v.reason}" for v in votes]


# --- suites ------------------------------------------------------------------------------------


def item_is_destructive(item: SuiteItem) -> bool:
    compiled = map_action(item)
    if compiled is None:
        return False
    return approval_kind(compiled.primitive, dict(compiled.args)) is not None


def suite_view(suite: Suite) -> SuiteView:
    return SuiteView(
        source=suite.source,
        title=suite.title,
        items=[
            SuiteItemView(
                n=item.n,
                title=item.title,
                action=item.action,
                cycles=item.cycles,
                destructive=item_is_destructive(item),
                sentence=item.sentence(),
                approved=item.approved,
                params=dict(item.params),
            )
            for item in suite.items
        ],
        sentence=suite.sentence(),
        problem=None,
    )


def suite_problem(filename: str, message: ThreePartMessage) -> SuiteView:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return SuiteView(
        source=filename,
        title=stem.replace("-", " ").replace("_", " ") or filename,
        items=[],
        sentence=message.what_happened,
        problem=problem_sentence(message),
    )


def suite_from_view(view: SuiteView) -> Suite:
    """The wizard's rows back into the agent's `Suite`; the parse is lossless because the
    view carries the action and the parameters as parsed."""
    return Suite(
        title=view.title,
        source=view.source,
        items=[
            SuiteItem(
                n=index,
                title=item.title,
                action=item.action,
                params=dict(item.params),
                cycles=item.cycles,
                approved=item.approved,
            )
            for index, item in enumerate(view.items, start=1)
        ],
    )


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ").strip()


def suite_to_markdown(view: SuiteView) -> str:
    """A suite as the wizard confirmed it, as the `.md` table `parse_suite_md` reads back into
    the same items — so `Agent.ingest` inside the kernel sees exactly what the person saw."""
    lines = [
        f"# {_cell(view.title)}",
        "",
        "| Step | Action | Parameters | Cycles | Approved |",
        "|---|---|---|---|---|",
    ]
    for item in view.items:
        params = "; ".join(f"{key}={_cell(value)}" for key, value in item.params.items())
        lines.append(
            f"| {_cell(item.title)} | {_cell(item.action)} | {params} | {item.cycles} | "
            f"{'yes' if item.approved else ''} |"
        )
    return "\n".join(lines) + "\n"


# --- targets -----------------------------------------------------------------------------------


def target_views(rows: list[dict[str, Any]]) -> list[TargetView]:
    views: list[TargetView] = []
    for row in rows:
        ref = str(row.get("ref") or row.get("alias") or "")
        if not ref:
            continue
        holder = row.get("holder")
        views.append(
            TargetView(
                ref=ref,
                model=str(row.get("model") or "unknown"),
                free=bool(row.get("free", holder is None)),
                holder=str(holder) if holder else None,
                armed=bool(row.get("armed", False)),
                sentence=str(row.get("sentence") or f"{ref} is {'busy' if holder else 'free'}."),
            )
        )
    return views


# --- plans -------------------------------------------------------------------------------------


def plan_preview(plan: Plan, guardrails: Guardrails, cross_check: Any) -> PlanPreview:
    destructive = plan.destructive_steps()
    return PlanPreview(
        sentence=plan.summary,
        steps=[
            PlanStepView(id=s.id, title=s.title, destructive=s.needs_approval) for s in plan.steps
        ],
        destructive_steps=[s.title for s in destructive],
        guardrails=guardrails.sentences(),
        cross_check=cross_check,
        step_count=len(plan.steps),
        cycle_count=sum(1 for s in plan.steps if s.primitive == "power_cycle"),
    )


def waiting_cells(plan: Plan | None) -> list[CycleCellView]:
    """The LED map before the executor has drawn one: every power cycle, waiting."""
    if plan is None:
        return []
    return [
        CycleCellView(n=int(s.args.get("cycle", s.n)), kind=str(s.args.get("kind", "dc")))
        for s in plan.steps
        if s.primitive == "power_cycle"
    ]


# --- runs --------------------------------------------------------------------------------------


def run_sentence(ticket: Ticket, state: RunState | None) -> str:
    if (
        state is not None
        and state.cycles
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


def run_view(ticket: Ticket, state: RunState | None, console_tail: list[str]) -> RunView:
    plan = ticket.plan
    pending: list[str] = []
    for approval in ticket.pending_approvals:
        try:
            pending.append(plan.step(approval.step_id).title if plan else approval.step_id)
        except KeyError:
            pending.append(approval.step_id)
    cells = (
        [
            CycleCellView(n=c.n, kind=c.kind, status=c.status, sentence=c.sentence)
            for c in state.cycles
        ]
        if state is not None and state.cycles
        else waiting_cells(plan)
    )
    target = ticket.job.target.ref if ticket.job.target else (state.target if state else "")
    return RunView(
        ticket_id=ticket.id,
        title=ticket.title,
        target=target,
        state=ticket.state.value,
        sentence=run_sentence(ticket, state),
        pending_approvals=pending,
        cells=cells,
        console_tail=list(console_tail),
        findings=[f.headline() for f in ticket.findings],
        votes=vote_sentences(ticket.votes),
        finding_details=[
            FindingRef(sentence=f.issue, owner=f.owner or "unassigned", ticket_id=f.ticket_id)
            for f in ticket.findings
        ],
        created_at=ticket.created_at.isoformat(),
    )
