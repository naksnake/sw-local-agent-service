"""SOP: one structured source, two renderings, always together (CLAUDE.md §5.5, INV-13).

Phase 2 placeholder: the structure, identifiers, commands and numbers are copied by code
into both files; the section headings are in each language; English prose is kept verbatim
in the Chinese rendering with a note that the Phase 5 translation step adds the reviewed
prose. The renderer never invents a translation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from slas_schemas.common import Lang
from slas_schemas.envfile import write_atomic
from slas_schemas.sop import SopModel, SopStep, SopTemplate
from slas_schemas.ticket import SopRefs, Ticket, TicketState

_HEADINGS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "language": "Language: English (en)",
        "notice": (
            "> Placeholder rendering: structure, identifiers, commands and numbers are copied "
            "by code. The Phase 5 renderer adds the reviewed prose."
        ),
        "purpose": "Purpose",
        "prerequisites": "Prerequisites",
        "steps": "Steps",
        "step_columns": "| Step | Action | Expected | Evidence |",
        "checks": "Checks",
        "results": "Results",
        "findings": "Findings",
        "next_actions": "Next actions",
        "glossary": "Glossary references",
        "none": "None.",
    },
    "zh-Hant": {
        "language": "語言：繁體中文（zh-Hant）",
        "notice": (
            "> 佔位版本：結構、識別碼、指令與數值由程式直接複製；英文原文暫予保留，"
            "中文譯文將於第五階段由翻譯流程加入。"
        ),
        "purpose": "目的",
        "prerequisites": "前置條件",
        "steps": "步驟",
        "step_columns": "| 步驟 | 動作 | 預期結果 | 證據 |",
        "checks": "檢查項目",
        "results": "結果",
        "findings": "發現",
        "next_actions": "後續行動",
        "glossary": "術語參照",
        "none": "無。",
    },
}


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
    findings = [finding.headline() for finding in ticket.findings]
    next_actions = (
        ["Review the findings and decide whether a bug ticket is needed."] if findings else []
    )
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


def _bullets(items: list[str], none: str) -> list[str]:
    return [f"- {item}" for item in items] if items else [none]


def render_markdown(model: SopModel, lang: Lang) -> str:
    if lang not in _HEADINGS:
        raise ValueError(f"no Phase 2 rendering for {lang}; zh-Hans arrives with Phase 5")
    text = _HEADINGS[lang]
    lines = [f"# {model.title}", "", text["language"], "", text["notice"], ""]
    lines += [f"## {text['purpose']}", "", model.purpose or text["none"], ""]
    lines += [f"## {text['prerequisites']}", "", *_bullets(model.prerequisites, text["none"]), ""]
    lines += [f"## {text['steps']}", ""]
    if model.steps:
        lines += [text["step_columns"], "|---|---|---|---|"]
        for step in model.steps:
            evidence = ", ".join(step.evidence)
            lines.append(f"| {step.n} | {step.action} | {step.expected} | {evidence} |")
    else:
        lines.append(text["none"])
    lines += ["", f"## {text['checks']}", "", *_bullets(model.checks, text["none"]), ""]
    lines += [f"## {text['results']}", ""]
    lines += [f"- {key}: {value}" for key, value in model.results.items()] or [text["none"]]
    lines += ["", f"## {text['findings']}", "", *_bullets(model.findings, text["none"]), ""]
    lines += [f"## {text['next_actions']}", "", *_bullets(model.next_actions, text["none"]), ""]
    lines += [f"## {text['glossary']}", "", *_bullets(model.glossary_refs, text["none"]), ""]
    return "\n".join(lines)


def render_sop(model: SopModel, out_dir: Path) -> SopRefs:
    """Write sop.en.md, sop.zh-Hant.md and sop.json together — never one without the others."""
    out_dir.mkdir(parents=True, exist_ok=True)
    en = out_dir / "sop.en.md"
    zh = out_dir / "sop.zh-Hant.md"
    data = out_dir / "sop.json"
    write_atomic(en, render_markdown(model, "en"), mode=0o644)
    write_atomic(zh, render_markdown(model, "zh-Hant"), mode=0o644)
    write_atomic(data, model.model_dump_json(indent=2) + "\n", mode=0o644)
    return SopRefs(en=str(en), zh=str(zh), data=str(data))
