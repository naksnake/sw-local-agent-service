"""Skill expansion happens once, in the kernel, at PLAN (§5.1, ADR-0013).

A plan step `skill` reaches the executor already compiled; the executor never compiles. The
gate refuses a skill that is missing, not allowed for the agent, or not turned on here, and
the ticket stops at PLAN with one sentence. A destructive skill makes its plan step
destructive, so the INV-7 approval is requested before anything runs."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slas_kernel.clock import FakeClock
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.skills import SkillGate, compiled_from_step
from slas_kernel.store import MemoryTicketStore
from slas_schemas.common import AgentName
from slas_schemas.job import InputRef, Job, MesTicket, Upload
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import Observation, TicketState
from slas_skills.library import SEL_COLLECT_CLEAR, STATION_LOGIN_BURNIN
from slas_skills.schema import Skill, parse_skill
from slas_skills.state import SkillStateStore

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
UPLOAD = Upload(filename="suite.md", uploaded_by="pat", content="# suite\n", size_bytes=8)

POWER_DOWN: dict[str, Any] = {
    "skill": {
        "id": "sel-then-power-off",
        "name": "Save the event log, then power the unit off",
        "version": "1.0.0",
        "agents": ["validation"],
        "requires": ["redfish"],
        "inputs": {"target": {"type": "target_ref", "required": True}},
        "steps": [
            {"redfish": {"target": "{{ target }}", "action": "get_sel"}, "id": "sel"},
            {"redfish": {"target": "{{ target }}", "action": "power_off"}, "id": "off"},
        ],
        "on_failure": "stop",
    }
}


class SkillAgent(NullAgent):
    """A Validation-flavoured agent whose plan is one `skill` step; nothing else."""

    name: AgentName = "validation"

    def __init__(self, skill_id: str, **extra: object) -> None:
        self.skill_id = skill_id
        self.extra = extra

    def ingest(self, raw: Upload | MesTicket) -> Job:
        assert isinstance(raw, Upload)
        digest = hashlib.sha256(raw.filename.encode()).hexdigest()
        return Job(
            id=f"job-{digest[:12]}",
            agent=self.name,
            user=raw.uploaded_by,
            title=f"Run {self.skill_id}",
            inputs=[InputRef(name=raw.filename, kind="upload", sha256=digest)],
            created_at=NOW,
        )

    def plan(self, job: Job) -> Plan:
        step = Step(
            id="use-skill",
            n=1,
            primitive="skill",
            title=f"Run the skill {self.skill_id}",
            args={"skill_id": self.skill_id, "target": "gx8-01", **self.extra},
            risk="caution",
        )
        return Plan(
            id=f"plan-{job.id}", job_id=job.id, summary=step.title, steps=[step], created_at=NOW
        )


class CompiledOnlyExecutor:
    """Performs a `skill` step only from the compiled steps the gate stored; never compiles."""

    def __init__(self) -> None:
        self.seen: list[Step] = []

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        if step.primitive != "skill":
            raise UnknownPrimitiveError(step)
        self.seen.append(step)
        compiled = compiled_from_step(step)
        if compiled is None:
            return Observation(exit_code=2, summary="no compiled steps reached the executor")
        titles = "; ".join(s.title for s in compiled.plan.steps)
        return Observation(exit_code=0, summary=f"{len(compiled.plan.steps)} steps: {titles}")


def library(*mappings: dict[str, Any]) -> dict[str, Skill]:
    skills = [parse_skill(m) for m in mappings]
    return {skill.id: skill for skill in skills}


def make(tmp_path: Path, agent: SkillAgent, *skills: dict[str, Any], on: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    lib = library(*skills)
    state = SkillStateStore(tmp_path / "Skills" / "library")
    for skill in lib.values():
        state.record_import(skill, by="lee", now=NOW)
    for skill_id in on:
        state.enable(lib[skill_id], "validation", by="lee", now=NOW)
    executor = CompiledOnlyExecutor()
    kernel = Kernel(
        data_root=tmp_path,
        agent=agent,
        executor=executor,
        store=MemoryTicketStore(),
        clock=FakeClock(),
        skill_gate=SkillGate(library=lib, state=state, clock=FakeClock()),
    )
    return kernel, executor, state


def test_the_gate_compiles_an_enabled_skill_once_and_the_executor_only_performs_it(
    tmp_path: Path,
) -> None:
    kernel, executor, _ = make(
        tmp_path, SkillAgent("sel-collect-clear"), SEL_COLLECT_CLEAR, on=("sel-collect-clear",)
    )
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.DONE
    assert ticket.plan is not None
    (step,) = ticket.plan.steps
    compiled = compiled_from_step(step)
    assert compiled is not None and compiled.skill_id == "sel-collect-clear"
    assert [s.primitive for s in compiled.plan.steps] == ["redfish", "copy", "assert"]
    assert compiled.plan.steps[0].args["target"] == "gx8-01", "the plan's target bound the input"
    assert step.risk == "caution", "a safe skill never lowers the plan step's own risk"
    assert step.args["skill_id"] == "sel-collect-clear", "the original arguments stay"
    (performed,) = executor.seen
    assert performed.args["compiled"] == step.args["compiled"]
    record = ticket.step_record("use-skill")
    assert record is not None and record.observation is not None
    assert record.observation.summary == (
        "3 steps: Redfish get_sel on gx8-01; Copy; Check SEL nearly full before clearing"
    )


def test_a_skill_turned_off_stops_the_ticket_at_plan_with_the_adr_sentence(tmp_path: Path) -> None:
    kernel, executor, state = make(tmp_path, SkillAgent("sel-collect-clear"), SEL_COLLECT_CLEAR)
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.FAILED and ticket.plan is None
    assert ticket.history[-1].reason == (
        "Collect and clear the BMC event log is not turned on for the Validation Agent."
    )
    assert executor.seen == [], "nothing ran"
    journal = kernel.journal_for(ticket.id).entries()
    note = next(e for e in journal if e.payload.get("skill_gate"))
    assert note.payload["skill_gate"]["what_to_do"] == (
        "Turn it on under Skills, or remove it from the plan."
    )
    assert note.payload["step"] == "use-skill"
    # Turning it on needs no restart: the same kernel runs the next ticket.
    state.enable(parse_skill(SEL_COLLECT_CLEAR), "validation", by="lee", now=NOW)
    assert kernel.run(UPLOAD).state is TicketState.DONE


def test_missing_and_disallowed_skills_are_refused_with_their_own_sentences(
    tmp_path: Path,
) -> None:
    kernel, _, _ = make(tmp_path, SkillAgent("nope"), SEL_COLLECT_CLEAR, on=("sel-collect-clear",))
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.FAILED
    assert ticket.history[-1].reason == "The skill nope is not in this installation's library."

    # station-login-burnin is a Factory skill; the Validation Agent may not use it even if on.
    kernel, _, _ = make(tmp_path / "b", SkillAgent("station-login-burnin"), STATION_LOGIN_BURNIN)
    ticket = kernel.run(UPLOAD)
    assert ticket.history[-1].reason == (
        "Log in to the test station and start BurnIn does not work with the Validation Agent."
    )
    entries = kernel.journal_for(ticket.id).entries()
    note = next(e for e in entries if e.payload.get("skill_gate"))
    assert note.payload["skill_gate"]["likely_cause"] == "Its author allows only the Factory Agent."


def test_a_compile_problem_is_reported_in_three_parts_not_raised(tmp_path: Path) -> None:
    agent = SkillAgent("sel-collect-clear", inputs={"unknown": 1})
    kernel, _, _ = make(tmp_path, agent, SEL_COLLECT_CLEAR, on=("sel-collect-clear",))
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.FAILED
    assert ticket.history[-1].reason == (
        "Collect and clear the BMC event log was given inputs it does not declare: unknown."
    )


def test_a_destructive_skill_makes_its_plan_step_wait_for_approval(tmp_path: Path) -> None:
    kernel, executor, _ = make(
        tmp_path, SkillAgent("sel-then-power-off"), POWER_DOWN, on=("sel-then-power-off",)
    )
    ticket = kernel.run(UPLOAD)
    assert ticket.state is TicketState.PLANNED, "INV-7: nothing runs before a person decides"
    assert ticket.plan is not None and ticket.plan.steps[0].risk == "destructive"
    assert [a.step_id for a in ticket.approvals] == ["use-skill"]
    assert executor.seen == []
    compiled = compiled_from_step(ticket.plan.steps[0])
    assert compiled is not None
    assert [s.risk for s in compiled.plan.steps] == ["safe", "destructive"]
    kernel.approve(ticket.id, "use-skill", decided_by="lee", note="line is clear")
    assert kernel.resume(ticket.id).state is TicketState.DONE
    assert len(executor.seen) == 1


def test_without_a_gate_the_kernel_leaves_plans_alone_and_secrets_stay_handles(
    tmp_path: Path,
) -> None:
    lib = library(STATION_LOGIN_BURNIN)
    state = SkillStateStore(tmp_path)
    skill = lib["station-login-burnin"]
    state.record_import(skill, by="lee", now=NOW)
    state.enable(skill, "factory", by="lee", now=NOW)
    gate = SkillGate(library=lib, state=state, clock=FakeClock())
    step = Step(
        id="login",
        n=1,
        primitive="skill",
        title="Log in",
        args={
            "skill_id": "station-login-burnin",
            "station": "station-07",
            "inputs": {"user": "operator"},
            "secret_refs": {"password": "env:STATION_OPERATOR_PASSWORD"},
        },
    )
    plan = Plan(id="p", job_id="j", summary="one skill", steps=[step], created_at=NOW)
    expanded = gate.expand(plan, agent="factory")
    compiled = compiled_from_step(expanded.steps[0])
    assert compiled is not None
    assert compiled.secret_handles == {"password": "secret://station-login-burnin/password"}
    dumped = expanded.model_dump_json()
    assert "env:STATION_OPERATOR_PASSWORD" in dumped, "the reference travels"
    assert "resolved-at-dispatch" not in dumped, "the placeholder never lands in the plan"
    assert expanded.steps[0].args["station"] == "station-07"
    # A plan with no skill step is returned as is, and a kernel without a gate changes nothing.
    plain = Plan(
        id="q",
        job_id="j",
        summary="no skills",
        steps=[Step(id="a", n=1, primitive="fake", title="Nothing")],
        created_at=NOW,
    )
    assert gate.expand(plain, agent="factory") is plain
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=CompiledOnlyExecutor(),
        store=MemoryTicketStore(),
    )
    assert kernel.skill_gate is None
