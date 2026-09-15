"""SOP: the kernel builds the structured source from the ticket; `slas_sop` renders it.

One structured source, two renderings, always together (CLAUDE.md §5.5, INV-13). The
agent contributes only its `SopTemplate`; the kernel fills steps, results and findings from
the ticket, and `slas_sop.render.render_sop` writes sop.en.md, sop.zh-Hant.md and sop.json.
"""

from __future__ import annotations

from slas_schemas.sop import SopModel, SopStep, SopTemplate
from slas_schemas.ticket import Ticket, TicketState
from slas_sop.render import UnsupportedLanguageError, render_markdown, render_sop

__all__ = ["UnsupportedLanguageError", "build_sop_model", "render_markdown", "render_sop"]


def build_sop_model(
    ticket: Ticket, template: SopTemplate, *, final_state: TicketState | None = None
) -> SopModel:
    """Build the structured SOP; `final_state` is the state the ticket is about to enter."""
    steps = [
        SopStep(
            n=record.n,
            action=record.title,
            expected=record.verdict.sentence if record.verdict else "",
            evidence=[f"step {record.step_id}", f"exit {record.observation.exit_code}"]
            if record.observation is not None
            else [f"step {record.step_id}"],
        )
        for record in ticket.steps
    ]
    results = {"ticket": ticket.id, "state": (final_state or ticket.state).value}
    if ticket.rca is not None:
        results["rca"] = ticket.rca.cause
        if ticket.rca.fingerprint:
            results["fingerprint"] = ticket.rca.fingerprint
    findings = [finding.headline() for finding in ticket.findings]
    next_actions: list[str] = []
    if findings:
        next_actions.append("Review the findings and decide whether a bug ticket is needed.")
    if ticket.rca is not None and ticket.rca.uncertain and findings:
        next_actions.append("The root cause is marked uncertain; confirm it before acting on it.")
    return SopModel(
        title=f"{ticket.title} — {ticket.id}",
        purpose=template.purpose,
        prerequisites=[f"Agent: {ticket.agent}", f"Requested by: {ticket.user}"],
        steps=steps,
        checks=list(template.checks),
        results=results,
        findings=findings,
        next_actions=next_actions,
        glossary_refs=[],
    )
