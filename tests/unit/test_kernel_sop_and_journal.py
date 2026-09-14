"""The two-language SOP without a translator (INV-13) and the journal's own behaviour."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.journal import Journal
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.rca import fingerprint
from slas_kernel.sop import render_markdown
from slas_kernel.store import FileTicketStore
from slas_schemas.job import Upload
from slas_schemas.sop import SopModel, SopStep

IDENTIFIER = re.compile(r"T-null-\d{4}|\bs[1-5]\b|\bexit \d+\b|\b\d+\b")


def run_null_agent(tmp_path: Path) -> tuple[Kernel, str]:
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=FakeExecutor(),
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
    )
    ticket = kernel.run(Upload(filename="plan.md", uploaded_by="pat"))
    return kernel, ticket.id


def test_sop_is_rendered_in_both_languages_from_one_source(tmp_path: Path) -> None:
    _, ticket_id = run_null_agent(tmp_path)
    sop_dir = tmp_path / "SOP" / ticket_id
    en = (sop_dir / "sop.en.md").read_text(encoding="utf-8")
    zh = (sop_dir / "sop.zh-Hant.md").read_text(encoding="utf-8")
    data = json.loads((sop_dir / "sop.json").read_text(encoding="utf-8"))

    assert en.startswith(f"# Rehearsal for plan.md — {ticket_id}\n")
    assert zh.startswith(f"# Rehearsal for plan.md — {ticket_id}\n")
    assert "## Steps" in en and "| Step | Action | Expected | Evidence |" in en
    assert "## 步驟" in zh and "| 步驟 | 動作 | 預期結果 | 證據 |" in zh
    for heading in ("目的", "前置條件", "檢查項目", "結果", "發現", "後續行動", "術語參照"):
        assert f"## {heading}" in zh
    assert "Placeholder rendering" in en and "佔位版本" in zh

    # Identifiers, commands and numbers are copied by code and identical in both (INV-13).
    # The placeholder notice and the language line are the only prose that differs by design.
    def body(text: str) -> str:
        return "\n".join(
            line
            for line in text.splitlines()
            if not line.startswith(">") and not line.startswith(("Language:", "語言"))
        )

    assert IDENTIFIER.findall(body(en)) == IDENTIFIER.findall(body(zh))
    assert IDENTIFIER.findall(body(en)), "the identifier check must have something to compare"
    assert "| 1 | Say hello | Say hello finished as expected. | step s1, exit 0 |" in en
    assert "| 1 | Say hello | Say hello finished as expected. | step s1, exit 0 |" in zh

    # The structured source matches what was rendered.
    model = SopModel.model_validate(data)
    assert [step.action for step in model.steps] == [
        "Say hello",
        "List the inputs",
        "Count to three",
        "Warn on stderr",
        "Finish",
    ]
    assert model.results["ticket"] == ticket_id and model.results["state"] == "Done"
    assert model.checks == [
        "Every step has an observation.",
        "The journal has one observation per step.",
    ]


def test_render_markdown_handles_empty_sections_and_refuses_unrendered_languages() -> None:
    model = SopModel(title="Empty")
    en = render_markdown(model, "en")
    assert en.count("None.") == 8, (
        "purpose, prerequisites, steps, checks, results, findings, next actions, glossary"
    )
    zh = render_markdown(model, "zh-Hant")
    assert zh.count("無。") == 8
    full = SopModel(
        title="Full",
        purpose="p",
        steps=[SopStep(n=1, action="a", expected="e", evidence=["x", "y"])],
        results={"k": "v"},
        findings=["[Issue] f | [Owner] EE"],
        next_actions=["n"],
        glossary_refs=["baseline"],
    )
    text = render_markdown(full, "en")
    assert "| 1 | a | e | x, y |" in text and "- k: v" in text and "- baseline" in text
    with pytest.raises(ValueError, match="zh-Hans is not rendered yet"):
        render_markdown(model, "zh-Hans")


def test_fingerprint_masks_numbers_and_addresses() -> None:
    assert fingerprint("Xid 79 at 0x7f3a on GPU3") == fingerprint("Xid 31 at 0xdead on GPU7")
    assert fingerprint("Xid 79 at 0x7f3a on GPU3") != fingerprint("EDAC error on DIMM 3")
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint("anything"))


def test_journal_append_replays_and_finds_the_open_intent(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl", FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
    assert journal.entries() == []
    assert journal.interrupted_step() is None
    journal.append("state", "T-null-0001", {"state": "Open"})
    journal.append("intent", "T-null-0001", {"primitive": "fake"}, step_id="s1")
    assert journal.interrupted_step() == "s1"
    journal.append("observation", "T-null-0001", {"exit_code": 0}, step_id="s1")
    journal.append("intent", "T-null-0001", {}, step_id="s2")
    entries = journal.entries()
    assert [e.seq for e in entries] == [1, 2, 3, 4]
    assert entries[0].at.isoformat() == "2026-01-01T00:00:00+00:00"
    assert entries[1].at > entries[0].at
    assert journal.completed_steps().keys() == {"s1"}
    assert journal.interrupted_step() == "s2"
    assert [e.kind for e in journal.step_entries("s1")] == ["intent", "observation"]
    # The file is plain JSON lines an engineer can read with any tool.
    raw = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(raw[2]) == {
        "seq": 3,
        "at": "2026-01-01T00:00:02Z",
        "ticket_id": "T-null-0001",
        "kind": "observation",
        "step_id": "s1",
        "trace_id": None,
        "payload": {"exit_code": 0},
    }
