"""COMPILE (CLAUDE.md §10.2): suite.md / suite.xlsx → plan.yaml, with the reject rules
(unknown primitive · count > cap · destructive without approval flag) and the guardrails."""

from __future__ import annotations

import html
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_hal.primitives import PRIMITIVES, build_plan_schema, render_primitives_yaml
from slas_orchestrator.validation.agent import TargetNotChosenError, ValidationAgent
from slas_orchestrator.validation.compiler import (
    CompiledItem,
    FakeCompiler,
    PlanCompileError,
    compile_suite,
    map_action,
    render_plan_yaml,
    write_plan,
)
from slas_orchestrator.validation.suite import (
    SuiteError,
    SuiteItem,
    parse_suite_md,
    parse_suite_xlsx,
    read_xlsx_rows,
)
from slas_schemas.job import MesTicket, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import Observation
from slas_validation_executor.guardrails import (
    GuardrailError,
    Guardrails,
    check_plan,
    default_guardrails,
    guardrails_from_mapping,
)

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
TARGET = "lab-gx8-01"

TABLE_SUITE = """# GX8 DC cycling

Notes before the table are ignored.

| Step | Action | Parameters | Cycles | Approved |
|---|---|---|---|---|
| DC cycle | DC power cycle | settle_s=60 | 25 | |
| CPU stress | stress-ng | duration_s=120; args=--cpu 8 | | |
| Read SEL | SEL snapshot | | | |
"""

BULLET_SUITE = """# Quick check
- DC cycle x3, settle 60 s
- Warm reboot x2
- Collect logs
"""


def compile_text(text: str, **kwargs: object) -> Plan:
    suite = parse_suite_md(text)
    return compile_suite(
        suite,
        job_id="job-1",
        target=TARGET,
        guardrails=default_guardrails(),
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


# --- suites --------------------------------------------------------------------------------


def test_markdown_table_suite_is_parsed_by_header_name() -> None:
    suite = parse_suite_md(TABLE_SUITE)
    assert suite.title == "GX8 DC cycling"
    assert suite.sentence() == "GX8 DC cycling: 3 items, 27 cycles in total."
    dc, stress, sel = suite.items
    assert (dc.title, dc.action, dc.cycles, dc.params) == (
        "DC cycle",
        "DC power cycle",
        25,
        {"settle_s": "60"},
    )
    assert stress.params == {"duration_s": "120", "args": "--cpu 8"}
    assert sel.cycles == 1 and sel.approved is False
    assert dc.sentence() == "1. DC cycle ×25 (settle_s 60)"


def test_bullet_suite_reads_cycles_settle_and_approval_from_the_sentence() -> None:
    suite = parse_suite_md("- AC cycle x2, settle 45 s, approved\n- Baseline", source="ac.md")
    assert suite.title == "ac"
    ac, base = suite.items
    assert (ac.title, ac.cycles, ac.params, ac.approved) == (
        "AC cycle",
        2,
        {"settle_s": "45"},
        True,
    )
    assert (base.title, base.cycles, base.approved) == ("Baseline", 1, False)


def test_empty_and_itemless_suites_are_refused_in_three_parts() -> None:
    with pytest.raises(SuiteError) as info:
        parse_suite_md("   \n", source="suite.md")
    assert info.value.message.what_happened == "suite.md is empty."
    with pytest.raises(SuiteError) as info:
        parse_suite_md("# Title only\nprose, no items\n")
    assert info.value.message.what_happened == "suite.md has no items."
    assert "bullets such as" in info.value.message.likely_cause
    with pytest.raises(SuiteError) as info:
        parse_suite_md("| Foo | Bar |\n|---|---|\n| a | b |\n")
    assert "without a Step or Action column" in info.value.message.what_happened


def build_xlsx(path: Path, rows: list[list[object]], *, with_macro: bool = False) -> None:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared: list[str] = []
    cells_xml: list[str] = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(ord('A') + c)}{r}"
            if value == "" or value is None:
                continue  # Excel omits empty cells; the reader must place by `r`
            if isinstance(value, int | float):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            elif str(value).startswith("inline:"):
                text = html.escape(str(value)[len("inline:") :])
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
            else:
                if str(value) not in shared:
                    shared.append(str(value))
                cells.append(f'<c r="{ref}" t="s"><v>{shared.index(str(value))}</v></c>')
        cells_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = f'<worksheet xmlns="{ns}"><sheetData>{"".join(cells_xml)}</sheetData></worksheet>'
    sst = (
        f'<sst xmlns="{ns}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{html.escape(s)}</t></si>" for s in shared)
        + "</sst>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/sharedStrings.xml", sst)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        if with_macro:
            archive.writestr("xl/vbaProject.bin", b"\x00MACRO that must never run\x00")


def test_xlsx_suite_is_read_with_the_standard_library_and_macros_are_never_touched(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gx8-suite.xlsx"
    build_xlsx(
        path,
        [
            ["Step", "Action", "Parameters", "Cycles", "Approved"],
            ["DC cycle", "DC power cycle", "", 25, ""],
            ["Secure erase", "secure erase", "device=nvme0n1", "", "inline:yes"],
            ["", "", "", "", ""],
            ["Stress", "stress-ng", "duration_s=60", 1.0, ""],
        ],
        with_macro=True,
    )
    rows = read_xlsx_rows(path)
    assert rows[0] == ["Step", "Action", "Parameters", "Cycles", "Approved"]
    assert rows[1] == ["DC cycle", "DC power cycle", "", "25"]
    suite = parse_suite_xlsx(path)
    assert suite.title == "gx8 suite"
    dc, erase, stress = suite.items
    assert (dc.cycles, dc.params) == (25, {})
    assert (erase.params, erase.approved) == ({"device": "nvme0n1"}, True)
    assert (stress.cycles, stress.params) == (1, {"duration_s": "60"})


def test_bad_workbooks_are_three_part_errors(tmp_path: Path) -> None:
    not_zip = tmp_path / "suite.xlsx"
    not_zip.write_bytes(b"PK\x03\x04 this is not really a workbook")
    with pytest.raises(SuiteError) as info:
        parse_suite_xlsx(not_zip)
    assert info.value.message.what_happened == "suite.xlsx is not an .xlsx file."

    no_sheet = tmp_path / "empty.xlsx"
    with zipfile.ZipFile(no_sheet, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(SuiteError) as info:
        parse_suite_xlsx(no_sheet)
    assert info.value.message.what_happened == "empty.xlsx has no worksheet."

    damaged = tmp_path / "damaged.xlsx"
    with zipfile.ZipFile(damaged, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet><sheetData><row")
    with pytest.raises(SuiteError) as info:
        parse_suite_xlsx(damaged)
    assert info.value.message.what_happened == "damaged.xlsx could not be read."

    headers_only = tmp_path / "headers.xlsx"
    build_xlsx(headers_only, [["Step", "Action"]])
    with pytest.raises(SuiteError) as info:
        parse_suite_xlsx(headers_only)
    assert info.value.message.what_happened == "headers.xlsx has no items."


# --- compile ---------------------------------------------------------------------------------


def test_map_action_table_covers_the_known_verbs() -> None:
    def item(action: str, **params: str) -> SuiteItem:
        return SuiteItem(n=1, title=action, action=action, params=params)

    assert map_action(item("DC power cycle", settle_s="60")) == CompiledItem(
        primitive="power_cycle", args={"kind": "dc", "settle_s": 60}
    )
    assert map_action(item("AC cycle via PDU")) == CompiledItem(
        primitive="power_cycle", args={"kind": "ac"}
    )
    assert map_action(item("Warm reboot")) == CompiledItem(
        primitive="power_cycle", args={"kind": "warm"}
    )
    assert map_action(item("Flash the BMC", component="BMC", image="bmc-1.13")) == CompiledItem(
        primitive="firmware_flash", args={"component": "BMC", "image_ref": "bmc-1.13"}
    )
    assert map_action(item("Secure erase", device="nvme0n1")).primitive == "secure_erase"  # type: ignore[union-attr]
    assert map_action(item("Load BIOS defaults")).primitive == "bios_reset"  # type: ignore[union-attr]
    assert map_action(item("RAID rebuild", layout="raid10")).args == {"layout": "raid10"}  # type: ignore[union-attr]
    assert map_action(item("Check LnkSta of every GPU")).primitive == "inventory_snapshot"  # type: ignore[union-attr]
    assert map_action(item("Run nvqual", args="--long", duration_s="300")) == CompiledItem(
        primitive="run_diag", args={"tool": "nvqual", "args": ["--long"], "timeout_s": 300}
    )
    assert map_action(item("Burn the CPUs", duration_s="120")) == CompiledItem(
        primitive="stress", args={"tool": "stress-ng", "duration_s": 120}
    )
    assert map_action(item("Dance the macarena")) is None


def test_compile_unrolls_cycles_and_wraps_them_in_lease_console_baseline_collect_release() -> None:
    plan = compile_text(TABLE_SUITE)
    assert plan.id == "plan-job-1" and plan.job_id == "job-1"
    assert len(plan.steps) == 32
    assert [s.primitive for s in plan.steps[:3]] == [
        "lease_target",
        "console_on",
        "baseline_snapshot",
    ]
    cycles = [s for s in plan.steps if s.primitive == "power_cycle"]
    assert [s.id for s in cycles][:2] == ["cycle-001", "cycle-002"]
    assert cycles[13].title == "DC cycle 14 of 25"
    assert cycles[13].args == {
        "target": TARGET,
        "kind": "dc",
        "cycle": 14,
        "settle_s": 60,
        "total_cycles": 25,
    }
    assert all(s.risk == "caution" for s in cycles)
    stress = plan.step("item-2")
    assert stress.args == {
        "target": TARGET,
        "tool": "stress-ng",
        "args": ["--cpu", "8"],
        "duration_s": 120,
    }
    assert plan.step("item-3").primitive == "sel_snapshot"
    assert [s.primitive for s in plan.steps[-2:]] == ["collect_logs", "release_target"]
    assert plan.destructive is False
    assert plan.summary == (
        "GX8 DC cycling on lab-gx8-01: 25 power cycles and 3 suite items, 32 steps. "
        "Nothing destructive."
    )
    assert all(s.args["target"] == TARGET for s in plan.steps)


def test_compile_raises_the_settle_floor_and_maps_warm_reboots() -> None:
    plan = compile_text("- DC cycle x2, settle 5 s\n- Warm reboot x2\n")
    cycles = [s for s in plan.steps if s.primitive == "power_cycle"]
    assert [(s.args["kind"], s.args["settle_s"]) for s in cycles] == [
        ("dc", 10),
        ("dc", 10),
        ("warm", 10),
        ("warm", 10),
    ]
    assert cycles[2].title == "WARM cycle 3 of 4"


def test_reject_unknown_action_even_when_a_model_compiler_suggests_one() -> None:
    with pytest.raises(PlanCompileError) as info:
        compile_text("- Dance the macarena\n")
    assert info.value.message.what_happened == (
        "Step 1 (Dance the macarena) names no known action: 'Dance the macarena'."
    )
    assert "plans/primitives/validation.yaml" in info.value.message.likely_cause

    compiler = FakeCompiler({"dance the macarena": CompiledItem(primitive="shell", args={})})
    with pytest.raises(PlanCompileError) as info:
        compile_text("- Dance the macarena\n", compiler=compiler)
    assert "names no known action: 'shell'" in info.value.message.what_happened
    assert compiler.asked == ["Dance the macarena"]

    accepted = FakeCompiler({"dance the macarena": CompiledItem(primitive="sel_snapshot", args={})})
    plan = compile_text("- Dance the macarena\n", compiler=accepted)
    assert plan.step("item-1").primitive == "sel_snapshot"


def test_reject_missing_arguments_for_a_primitive() -> None:
    compiler = FakeCompiler({"wipe it": CompiledItem(primitive="secure_erase", args={})})
    with pytest.raises(PlanCompileError) as info:
        compile_text("- Wipe it, approved\n", compiler=compiler)
    assert info.value.message.what_happened == (
        "Step 1 (Wipe it) is missing device for secure_erase."
    )
    assert info.value.message.likely_cause == "secure_erase needs device."


def test_reject_destructive_steps_the_suite_does_not_flag_as_approved() -> None:
    with pytest.raises(PlanCompileError) as info:
        compile_text("- AC cycle x2\n")
    assert info.value.message.what_happened == (
        "Step 1 (AC cycle) is destructive (ac_cycle) and the suite does not flag it as approved."
    )
    assert "INV-7" in info.value.message.likely_cause
    with pytest.raises(PlanCompileError) as info:
        compile_text("- Flash the BMC\n")
    assert "is destructive (firmware_flash)" in info.value.message.what_happened


def test_an_approved_ac_plan_carries_destructive_steps_and_the_ac_settle_floor() -> None:
    plan = compile_text("- AC cycle x2, approved\n")
    ac = plan.destructive_steps()
    assert [s.id for s in ac] == ["cycle-001", "cycle-002"]
    assert all(s.risk == "destructive" and s.args["settle_s"] == 30 for s in ac)
    assert plan.summary.endswith("2 destructive steps need your approval before the run starts.")
    assert plan.sentence() == ("7 steps; 2 need your approval: AC cycle 1 of 2, AC cycle 2 of 2.")


def test_reject_more_cycles_than_the_guardrail_allows() -> None:
    with pytest.raises(PlanCompileError) as info:
        compile_text("- DC cycle x60\n- Warm reboot x41\n")
    assert info.value.message.what_happened == (
        "The suite asks for 101 power cycles; the limit is 100 per run."
    )
    assert "config/guardrails.yaml" in info.value.message.likely_cause
    plan = compile_text("- DC cycle x60\n- Warm reboot x40\n")
    assert len([s for s in plan.steps if s.primitive == "power_cycle"]) == 100


def test_reject_a_plan_that_would_exceed_the_step_cap() -> None:
    bullets = "\n".join(f"- Read SEL number {n}" for n in range(1, 200))
    with pytest.raises(PlanCompileError) as info:
        compile_text(bullets)
    assert info.value.message.what_happened == "The plan would have more than 200 steps."


def test_plan_yaml_is_rendered_and_written_with_its_json_twin(tmp_path: Path) -> None:
    plan = compile_text(BULLET_SUITE)
    text = render_plan_yaml(plan)
    assert text.startswith("# Validation plan rendered by SW Local Agent Service;\n")
    assert "id: plan-job-1\njob_id: job-1\nsummary: " in text
    assert (
        '  - id: cycle-001\n    n: 4\n    primitive: power_cycle\n    title: "DC cycle 1 of 5"'
        in text
    )
    assert (
        '      target: "lab-gx8-01"\n      kind: "dc"\n      cycle: 1\n      settle_s: 60' in text
    )
    path = write_plan(plan, tmp_path / "Plans")
    assert path == tmp_path / "Plans" / "plan-job-1" / "plan.yaml"
    assert path.read_text(encoding="utf-8") == text
    twin = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert Plan.model_validate(twin) == plan


def test_every_compiled_step_fits_the_plan_schema() -> None:
    schema = build_plan_schema()
    variants = {v["properties"]["primitive"]["const"]: v for v in schema["$defs"]["step"]["oneOf"]}
    assert set(variants) == set(PRIMITIVES)
    plan = compile_text(
        TABLE_SUITE
        + "| Flash BMC | Flash the BMC | component=BMC; image=bmc-1.13; approved | | yes |\n"
    )
    for step in plan.steps:
        args_schema = variants[step.primitive]["properties"]["args"]
        assert set(args_schema["required"]) <= set(step.args), step.id
        assert set(step.args) <= set(args_schema["properties"]), step.id
        assert step.risk in schema["$defs"]["step"]["properties"]["risk"]["enum"]
    assert schema["properties"]["steps"]["maxItems"] == 200
    assert "primitives:\n  lease_target:" in render_primitives_yaml()
    assert (
        "approval_kinds: [ac_cycle, firmware_flash, secure_erase, bios_reset, raid_reconfigure]"
        in render_primitives_yaml()
    )


# --- guardrails ------------------------------------------------------------------------------


def test_guardrails_are_sentences_and_refuse_nonsense() -> None:
    g = default_guardrails()
    assert g.sentences() == [
        "At most 100 power cycles per run.",
        "At least 10 s settle after a warm or DC cycle, 30 s after AC.",
        "A boot that takes longer than 15 minutes counts as failed.",
        "3 boot failures in a row abort the run.",
        "One run per target at a time.",
        "A run stops after 72 hours.",
        "Needs your approval every run: ac_cycle, firmware_flash, secure_erase, bios_reset, "
        "raid_reconfigure.",
    ]
    assert Guardrails(exclusive_lease=False).sentences()[4] == "Targets may be shared."
    with pytest.raises(GuardrailError) as info:
        guardrails_from_mapping({"max_cycles_per_run": 0}, source="config/guardrails.yaml")
    assert info.value.message.what_happened == (
        "The guardrails in config/guardrails.yaml could not be used."
    )
    assert (
        info.value.message.what_to_do
        == "Fix config/guardrails.yaml; every limit is a positive number."
    )


def test_check_plan_names_every_broken_guardrail() -> None:
    def step(n: int, primitive: str, risk: str, **args: object) -> Step:
        return Step(
            id=f"s{n}",
            n=n,
            primitive=primitive,
            title=f"{primitive} {n}",
            args={"target": TARGET, **args},
            risk=risk,
        )

    plan = Plan(
        id="p",
        job_id="j",
        summary="s",
        created_at=NOW,
        steps=[
            step(1, "power_cycle", "caution", kind="dc", cycle=1, settle_s=3),
            step(2, "power_cycle", "caution", kind="ac", cycle=2, settle_s=30),
            step(3, "firmware_flash", "caution", component="BMC", image_ref="x"),
            step(4, "sel_snapshot", "safe"),
        ],
    )
    problems = check_plan(plan, default_guardrails())
    assert problems == [
        "Step 1 settles 3 s after a DC cycle; the minimum is 10 s.",
        "Step 2 (power_cycle 2) is ac_cycle, which needs approval, but is not marked destructive.",
        "Step 3 (firmware_flash 3) is firmware_flash, which needs approval, but is not marked "
        "destructive.",
    ]
    tight = Guardrails(max_cycles_per_run=1)
    assert check_plan(plan, tight)[0] == "The plan has 2 power cycles; the limit is 1 per run."
    assert check_plan(compile_text("- DC cycle x2\n- Warm reboot x3\n"), default_guardrails()) == []


# --- the agent's wizard side ------------------------------------------------------------------


def test_validation_agent_ingests_a_suite_and_plans_once_a_target_is_chosen(
    tmp_path: Path,
) -> None:
    agent = ValidationAgent(plans_dir=tmp_path / "Plans", now=lambda: NOW)
    upload = Upload(filename="gx8.md", uploaded_by="pat", content=TABLE_SUITE, size_bytes=300)
    job = agent.ingest(upload)
    assert job.agent == "validation" and job.title == "GX8 DC cycling" and job.user == "pat"
    assert job.id.startswith("job-") and job.target is None
    assert job.inputs[0].name == "gx8.md" and job.inputs[0].sha256 is not None
    assert agent.suite(job.id).sentence() == "GX8 DC cycling: 3 items, 27 cycles in total."
    assert agent.destructive_items(job.id) == []
    with pytest.raises(TargetNotChosenError) as info:
        agent.plan(job)
    assert info.value.message.what_happened == "GX8 DC cycling has no target yet."

    chosen = agent.choose_target(job, TARGET)
    assert chosen.target is not None and chosen.target.ref == TARGET
    plan = agent.plan(chosen)
    assert plan.job_id == job.id and len(plan.steps) == 32
    assert (tmp_path / "Plans" / plan.id / "plan.yaml").is_file()
    # Ingesting the same upload again yields the same job id, now carrying the target.
    again = agent.ingest(upload)
    assert again.id == job.id and again.target is not None and again.target.ref == TARGET

    ac = agent.ingest(
        Upload(filename="ac.md", uploaded_by="pat", content="- AC cycle x2, approved\n")
    )
    assert agent.destructive_items(ac.id) == ["1. AC cycle ×2"]

    assert agent.verify(plan.steps[0], Observation(exit_code=0, summary="Leased.")).outcome == "ok"
    failed = agent.verify(plan.steps[0], Observation(exit_code=1))
    assert (failed.outcome, failed.sentence) == ("fail", "Lease lab-gx8-01 for this run finished.")
    template = agent.sop_template()
    assert (template.agent, template.kind) == ("validation", "verification")
    assert len(template.checks) == 4


def test_validation_agent_refuses_what_is_not_a_suite() -> None:
    agent = ValidationAgent()
    with pytest.raises(SuiteError) as info:
        agent.ingest(MesTicket(ticket_no="1", station="st-1", unit_sn="SN1", requested_by="mes"))
    assert info.value.message.what_happened == "The Validation Agent does not take MES tickets."
    with pytest.raises(SuiteError) as info:
        agent.ingest(Upload(filename="suite.xlsx", uploaded_by="pat"))
    assert info.value.message.what_happened == "suite.xlsx arrived without its file."
    with pytest.raises(SuiteError) as info:
        agent.ingest(Upload(filename="suite.md", uploaded_by="pat"))
    assert info.value.message.what_happened == "suite.md arrived without its content."
