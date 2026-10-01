"""The JSON the pages read: `CodingTask` and the Home ticket rows (contract §5, CLAUDE.md §9).

Everything here is derived from the ticket, its plan and its journal; nothing is stored
twice. Sentences, not enums, wherever a person reads them.
"""

from __future__ import annotations

from typing import Any, Final

from slas_kernel.journal import Journal
from slas_orchestrator.service.runs import RunState
from slas_schemas.journal import JournalEntry
from slas_schemas.plan import Plan
from slas_schemas.ticket import Ticket, TicketState

TOOLCHAIN_PRIMITIVE: Final = "toolchain"
MAX_FEED_LINES: Final = 400


def toolchain_sentence(plan: Plan | None) -> str | None:
    """The toolchain choice the Coding Agent recorded as its first step (§10.1)."""
    if plan is None:
        return None
    for step in plan.steps:
        if step.primitive == TOOLCHAIN_PRIMITIVE:
            sentence = step.args.get("sentence")
            return str(sentence) if sentence else step.title
    return None


def task_sentence(ticket: Ticket, run_state: RunState | None) -> str:
    if ticket.state is TicketState.FAILED:
        reason = ticket.history[-1].reason if ticket.history else "the run stopped."
        return f"{ticket.id} failed: {reason}"
    if ticket.state is TicketState.DONE:
        return f"{ticket.id} is done."
    if run_state is not None and run_state.error and not ticket.finished:
        return f"{ticket.id} stopped: {run_state.error}"
    return ticket.sentence()


def step_views(ticket: Ticket) -> list[dict[str, Any]]:
    """Plan steps with their status: the record's when the kernel wrote one, else pending."""
    if ticket.plan is None:
        return []
    statuses = {record.step_id: record.status for record in ticket.steps}
    return [
        {"n": step.n, "title": step.title, "status": statuses.get(step.id, "pending")}
        for step in ticket.plan.steps
    ]


def feed_from_journal(ticket: Ticket, entries: list[JournalEntry]) -> list[str]:
    """intent → "Doing <title>…", observation → its sentence, state → its reason."""
    titles: dict[str, str] = {}
    toolchain_step: str | None = None
    if ticket.plan is not None:
        titles = {step.id: step.title for step in ticket.plan.steps}
        toolchain_step = next(
            (s.id for s in ticket.plan.steps if s.primitive == TOOLCHAIN_PRIMITIVE), None
        )
    first = toolchain_sentence(ticket.plan)
    feed: list[str] = [first] if first else []
    for entry in entries:
        if entry.step_id is not None and entry.step_id == toolchain_step:
            continue  # the first line already says it
        if entry.kind == "intent" and entry.step_id is not None:
            title = titles.get(entry.step_id, entry.step_id)
            note = entry.payload.get("note")
            feed.append(f"Doing {title}…" + (f" ({note})" if note else ""))
        elif entry.kind == "observation":
            summary = str(entry.payload.get("summary") or "").strip()
            if not summary and entry.step_id is not None:
                record = ticket.step_record(entry.step_id)
                if record is not None and record.verdict is not None:
                    summary = record.verdict.sentence
            if summary:
                feed.append(summary)
        elif entry.kind == "state":
            reason = str(entry.payload.get("reason") or "").strip()
            if reason and reason != "ingested":
                feed.append(reason)
        elif entry.kind == "note":
            gate = entry.payload.get("skill_gate")
            if isinstance(gate, dict) and gate.get("what_happened"):
                feed.append(str(gate["what_happened"]))
            check = entry.payload.get("plan_cross_check")
            if check:
                feed.append(str(check))
    return feed[-MAX_FEED_LINES:]


def coding_task_view(
    ticket: Ticket,
    plan: Plan | None,
    journal: Journal | list[JournalEntry],
    run_state: RunState | None,
) -> dict[str, Any]:
    """The contract's `CodingTask`: ticket_id, title, state, sentence, steps, feed."""
    if plan is not None and ticket.plan is None:
        ticket = ticket.model_copy(update={"plan": plan})
    entries = journal.entries() if isinstance(journal, Journal) else list(journal)
    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "state": ticket.state.value,
        "sentence": task_sentence(ticket, run_state),
        "steps": step_views(ticket),
        "feed": feed_from_journal(ticket, entries),
    }


def ticket_row(ticket: Ticket, run_state: RunState | None = None) -> dict[str, Any]:
    """One row of the Home lists: id, agent, title, state, sentence, created_at."""
    return {
        "id": ticket.id,
        "agent": ticket.agent,
        "title": ticket.title,
        "state": ticket.state.value,
        "sentence": task_sentence(ticket, run_state),
        "created_at": ticket.created_at.isoformat(),
    }
