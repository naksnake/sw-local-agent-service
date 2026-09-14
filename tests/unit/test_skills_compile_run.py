"""COMPILE and RUN the §6.3 skills against the screen fake, a fake executor and the journal."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from slas_kernel.clock import FakeClock
from slas_kernel.journal import Journal
from slas_screen.backend import FakeScreen
from slas_screen.driver import ScreenDriver
from slas_screen.model import Point
from slas_screen.policy import ScreenPolicy
from slas_skills.compiler import SkillCompileError, compile_skill
from slas_skills.library import SEL_COLLECT_CLEAR, STATION_LOGIN_BURNIN
from slas_skills.runner import ApprovalRequiredError, FakeSkillExecutor, SkillRunner, StepOutcome
from slas_skills.schema import parse_skill

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
TICKET = "T-factory-0001"


def make_screen(tmp_path: Path, clock: FakeClock) -> tuple[ScreenDriver, FakeScreen]:
    fake = FakeScreen()
    fake.add_window("w1", "Login", "burnin-login", focused=True)
    fake.add_window("w2", "BurnIn v3.2", "burnin")
    fake.targets["#username"] = Point(x=40, y=40)
    fake.show_text("Start test")
    driver = ScreenDriver(
        fake,
        policy=ScreenPolicy(),
        clock=clock,
        screenshots_dir=tmp_path / "screens",
        sleep=lambda seconds: None,
    )
    return driver, fake


# --- compile ----------------------------------------------------------------------------------


def test_compile_station_login_binds_inputs_and_hands_out_a_secret_handle() -> None:
    skill = parse_skill(STATION_LOGIN_BURNIN)
    compiled = compile_skill(
        skill, {"station": "station-07", "password": "Sup3rSecret!"}, job_id="job-1", now=NOW
    )
    steps = compiled.plan.steps
    assert (
        len(steps) == 9
        and compiled.plan.summary == "Log in to the test station and start BurnIn v1.0.0"
    )
    assert steps[2].args == {"text": "operator"}, "the default input value is bound"
    assert steps[4].args == {"text": "secret://station-login-burnin/password"}
    assert "Sup3rSecret!" not in json.dumps(compiled.model_dump(mode="json")), (
        "the secret never enters the plan"
    )
    assert compiled.secret_steps == [steps[4].id]
    assert compiled.secret_handles == {"password": "secret://station-login-burnin/password"}
    assert steps[8].id == "status" and steps[8].args["target"] == "station-07"
    assert steps[8].args["command"] == ["burnin-ctl", "status", "--json"]
    assert compiled.outputs == {"burnin_status": "status"}
    assert [s.risk for s in steps] == ["safe"] * 8 + ["caution"]
    assert compiled.on_failure == "screenshot_and_stop"
    assert compiled.sentence() == "9 steps, none destructive."
    assert steps[0].title == "Focus window Login" and steps[7].title == "Click Start test"


def test_compile_reports_missing_and_unknown_inputs() -> None:
    skill = parse_skill(STATION_LOGIN_BURNIN)
    with pytest.raises(SkillCompileError) as missing:
        compile_skill(skill, {"station": "s"}, job_id="j", now=NOW)
    assert missing.value.message.what_happened == (
        "Log in to the test station and start BurnIn needs the input password."
    )
    with pytest.raises(SkillCompileError) as unknown:
        compile_skill(
            skill, {"station": "s", "password": "p", "colour": "red"}, job_id="j", now=NOW
        )
    assert "does not declare: colour" in unknown.value.message.what_happened
    with pytest.raises(SkillCompileError, match="refers to"):
        compile_skill(
            parse_skill(
                {
                    "skill": {
                        "id": "t",
                        "name": "T",
                        "version": "1.0.0",
                        "agents": ["coding"],
                        "steps": [{"wait": {"seconds": "{{ nope }}"}}],
                    }
                }
            ),
            {},
            job_id="j",
            now=NOW,
        )


def loop_skill(items: list[Any], *, mode: str = "gui") -> dict[str, Any]:
    return {
        "skill": {
            "id": "loops",
            "name": "Loops",
            "version": "1.0.0",
            "agents": ["coding"],
            "requires": ["screen"],
            "inputs": {
                "mode": {"type": "string", "default": mode},
                "count": {"type": "int", "default": 2},
            },
            "steps": [
                {
                    "foreach": {
                        "items": items,
                        "as": "board",
                        "then": [{"key": {"press": "F{{ board_index }}"}, "id": "press"}],
                    }
                },
                {
                    "if": {
                        "condition": "{{ mode }} == 'gui'",
                        "then": [{"click": {"text": "Start"}}],
                        "else": [{"wait": {"seconds": 1}}],
                    }
                },
                {
                    "if": {
                        "condition": "{{ steps.press.exit_code }} == 0",
                        "then": [{"screenshot": {"name": "ok"}}],
                        "else": [{"screenshot": {"name": "bad"}}],
                    }
                },
                {"assert": {"condition": "{{ count }} >= 2", "message": "count is at least 2"}},
            ],
        }
    }


def test_loops_unroll_with_unique_ids_and_run_time_conditions_are_kept() -> None:
    compiled = compile_skill(parse_skill(loop_skill(["a", "b", "c"])), {}, job_id="j", now=NOW)
    ids = [s.id for s in compiled.plan.steps]
    assert ids[:3] == ["press", "press-2", "press-3"]
    assert [s.args["press"] for s in compiled.plan.steps[:3]] == ["F0", "F1", "F2"]
    assert compiled.plan.steps[3].primitive == "click", (
        "the compile-time `if` picked the gui branch"
    )
    late = compiled.plan.steps[4:6]
    assert [s.primitive for s in late] == ["screenshot", "screenshot"]
    assert late[0].when == "{{ steps.press.exit_code }} == 0"
    assert late[1].when == "not {{ steps.press.exit_code }} == 0"
    assert compiled.plan.steps[-1].primitive == "assert"
    other = compile_skill(parse_skill(loop_skill(["a"], mode="cli")), {}, job_id="j", now=NOW)
    assert other.plan.steps[1].primitive == "wait", "the else branch when the input says cli"


def test_loop_and_plan_bounds() -> None:
    with pytest.raises(SkillCompileError, match="loops over 101 items; the limit is 100"):
        compile_skill(parse_skill(loop_skill(list(range(101)))), {}, job_id="j", now=NOW)
    big = {
        "skill": {
            "id": "big",
            "name": "Big",
            "version": "1.0.0",
            "agents": ["coding"],
            "steps": [
                {
                    "foreach": {
                        "items": list(range(100)),
                        "then": [
                            {"wait": {"seconds": 1}},
                            {"wait": {"seconds": 1}},
                            {"wait": {"seconds": 1}},
                        ],
                    }
                }
            ],
        }
    }
    with pytest.raises(SkillCompileError, match="expands to more than 200 steps"):
        compile_skill(parse_skill(big), {}, job_id="j", now=NOW)
    typed = parse_skill(
        {
            "skill": {
                "id": "t",
                "name": "T",
                "version": "1.0.0",
                "agents": ["coding"],
                "inputs": {"n": {"type": "int", "required": True}},
                "steps": [{"wait": {"seconds": "{{ n }}"}}],
            }
        }
    )
    with pytest.raises(SkillCompileError, match="must be a int"):
        compile_skill(typed, {"n": "many"}, job_id="j", now=NOW)
    assert compile_skill(typed, {"n": "3"}, job_id="j", now=NOW).plan.steps[0].args == {
        "seconds": 3
    }


# --- run --------------------------------------------------------------------------------------


def test_station_login_runs_against_the_screen_fake_with_screenshots_and_masked_secrets(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW, step=timedelta(milliseconds=200))
    driver, fake = make_screen(tmp_path, clock)
    journal = Journal(tmp_path / "journal.jsonl", clock)
    executor = FakeSkillExecutor()
    executor.script(
        "status",
        StepOutcome(
            ok=True,
            sentence="BurnIn is running.",
            exit_code=0,
            stdout='{"state": "running"}',
            outputs={"state": "running"},
        ),
    )
    compiled = compile_skill(
        parse_skill(STATION_LOGIN_BURNIN),
        {"station": "station-07", "password": "Sup3rSecret!"},
        job_id="j",
        now=NOW,
    )
    runner = SkillRunner(
        executor=executor, journal=journal, ticket_id=TICKET, screen=driver, sleep=lambda s: None
    )
    result = runner.run(compiled)

    assert result.status == "done"
    assert (
        result.sentence
        == "Log in to the test station and start BurnIn v1.0.0: 9 of 9 steps finished."
    )
    assert [run.status for run in result.steps] == ["done"] * 9
    assert result.outputs == {
        "burnin_status": {
            "state": "running",
            "exit_code": 0,
            "stdout": '{"state": "running"}',
            "stderr": "",
        }
    }
    # The screen fake saw the whole login, and the secret was typed but is nowhere in the journal.
    assert fake.typed == ["operator", "secret://station-login-burnin/password"]
    assert "Sup3rSecret!" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    entries = journal.entries()
    type_steps = [e for e in entries if e.step_id == compiled.secret_steps[0]]
    assert type_steps[0].payload["args"] == {"text": "[secret]"}
    # Every GUI step has a screenshot before and after, and the files exist.
    gui_runs = [run for run in result.steps if run.step_id != "status"]
    for run in gui_runs:
        assert len(run.screenshots) == 2, run
        for shot in run.screenshots:
            assert Path(shot).is_file() and Path(shot).stat().st_size > 0
    assert executor.calls[0].id == "status" and executor.calls[0].args["target"] == "station-07"
    kinds = [(e.kind, e.step_id) for e in entries]
    assert kinds.count(("intent", "status")) == 1 and kinds.count(("observation", "status")) == 1


def test_sel_collect_clear_resolves_step_outputs_at_run_time(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    journal = Journal(tmp_path / "journal.jsonl", clock)
    executor = FakeSkillExecutor()
    executor.script(
        "sel",
        StepOutcome(
            ok=True, sentence="SEL saved.", outputs={"file": "sel-0001.json", "count": 312}
        ),
    )
    compiled = compile_skill(
        parse_skill(SEL_COLLECT_CLEAR), {"target": "sut-3"}, job_id="j", now=NOW
    )
    assert compiled.plan.steps[1].args["from"] == "{{ steps.sel.file }}", (
        "late references survive compile"
    )
    runner = SkillRunner(executor=executor, journal=journal, ticket_id=TICKET)
    result = runner.run(compiled)
    assert result.status == "done"
    copy_call = next(step for step in executor.calls if step.primitive == "copy")
    assert copy_call.args == {"from": "sel-0001.json", "to": "logs/sel-before.json"}
    assert result.steps[2].sentence == "Checked: SEL nearly full before clearing."
    assert result.outputs["sel_file"]["file"] == "sel-0001.json"

    executor.script(
        "sel",
        StepOutcome(ok=True, sentence="SEL saved.", outputs={"file": "sel.json", "count": 5000}),
    )
    failed = SkillRunner(executor=executor, journal=journal, ticket_id=TICKET).run(compiled)
    assert failed.status == "failed"
    assert failed.steps[-1].sentence == "Check failed: SEL nearly full before clearing."
    assert failed.sentence == "Collect and clear the BMC event log v1.2.0 failed after step 3 of 3."


def test_a_destructive_step_needs_a_per_run_approval(tmp_path: Path) -> None:
    from tests.unit.test_skills_import import power_off_skill

    clock = FakeClock(NOW)
    journal = Journal(tmp_path / "journal.jsonl", clock)
    executor = FakeSkillExecutor()
    compiled = compile_skill(
        parse_skill(power_off_skill()), {"target": "sut-3"}, job_id="j", now=NOW
    )
    assert compiled.plan.destructive and compiled.plan.sentence() == (
        "2 steps; 1 needs your approval: Redfish power_off on sut-3."
    )
    runner = SkillRunner(executor=executor, journal=journal, ticket_id="T-validation-0001")
    with pytest.raises(ApprovalRequiredError) as raised:
        runner.run(compiled)
    assert raised.value.message.what_happened == (
        "Step 2 (Redfish power_off on sut-3) is destructive and has not been approved for this run."
    )
    assert [step.id for step in executor.calls] == ["before"], (
        "the safe step ran, the destructive one did not"
    )
    notes = [e.payload for e in journal.entries() if e.kind == "note"]
    assert {"approval_required": "Redfish power_off on sut-3"} in notes

    approved = SkillRunner(
        executor=FakeSkillExecutor(), journal=journal, ticket_id="T-validation-0001"
    )
    result = approved.run(compiled, approvals={"off"})
    assert result.status == "done" and [s.status for s in result.steps] == ["done", "done"]


def test_on_failure_policies(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    base = {
        "id": "f",
        "name": "F",
        "version": "1.0.0",
        "agents": ["coding"],
        "requires": ["files"],
        "steps": [
            {"run": {"command": ["make"]}, "id": "build"},
            {"run": {"command": ["make", "test"]}, "id": "test"},
        ],
    }
    executor = FakeSkillExecutor()
    executor.script("build", StepOutcome(ok=False, sentence="make exited with 2.", exit_code=2))

    stop = compile_skill(
        parse_skill({"skill": {**base, "on_failure": "stop"}}), {}, job_id="j", now=NOW
    )
    result = SkillRunner(
        executor=executor, journal=Journal(tmp_path / "a.jsonl", clock), ticket_id=TICKET
    ).run(stop)
    assert result.status == "failed" and len(result.steps) == 1

    cont = compile_skill(
        parse_skill({"skill": {**base, "on_failure": "continue"}}), {}, job_id="j", now=NOW
    )
    result = SkillRunner(
        executor=executor, journal=Journal(tmp_path / "b.jsonl", clock), ticket_id=TICKET
    ).run(cont)
    assert result.status == "done" and [s.status for s in result.steps] == ["done", "done"]

    retry = compile_skill(
        parse_skill({"skill": {**base, "on_failure": {"retry": {"max": 2, "delay_s": 5}}}}),
        {},
        job_id="j",
        now=NOW,
    )
    slept: list[float] = []
    result = SkillRunner(
        executor=executor,
        journal=Journal(tmp_path / "c.jsonl", clock),
        ticket_id=TICKET,
        sleep=slept.append,
    ).run(retry)
    assert result.status == "failed" and result.steps[0].attempts == 3 and slept == [5, 5]

    fake = FakeScreen()
    fake.add_window("w", "App")
    driver = ScreenDriver(
        fake,
        policy=ScreenPolicy(),
        clock=clock,
        screenshots_dir=tmp_path / "s",
        sleep=lambda s: None,
    )
    shot = compile_skill(
        parse_skill({"skill": {**base, "on_failure": "screenshot_and_stop"}}),
        {},
        job_id="j",
        now=NOW,
    )
    result = SkillRunner(
        executor=executor,
        journal=Journal(tmp_path / "d.jsonl", clock),
        ticket_id=TICKET,
        screen=driver,
    ).run(shot)
    assert result.status == "failed"
    assert (
        len(result.steps[0].screenshots) == 1 and "build-failure" in result.steps[0].screenshots[0]
    )


def test_screen_steps_without_a_display_fail_with_a_sentence(tmp_path: Path) -> None:
    compiled = compile_skill(
        parse_skill(STATION_LOGIN_BURNIN), {"station": "s", "password": "p"}, job_id="j", now=NOW
    )
    runner = SkillRunner(
        executor=FakeSkillExecutor(),
        journal=Journal(tmp_path / "j.jsonl", FakeClock(NOW)),
        ticket_id=TICKET,
    )
    result = runner.run(compiled)
    assert result.status == "failed"
    assert (
        result.steps[0].sentence
        == "Focus window Login needs a screen, and this run has no virtual display."
    )
