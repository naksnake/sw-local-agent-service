"""PLAN: skill expansion, the one place a `skill` step becomes concrete steps (§5.1, ADR-0013).

An agent's plan may name a skill (`primitive: skill`, args `skill_id`, `inputs`,
`secret_refs`, and the target the skill runs on). Before the plan is attached to the ticket
the kernel checks, deterministically, that the skill is in this installation's library, that
its author allows this agent, and that someone turned it on for this agent here; then it
compiles the skill once and stores the compiled steps on the plan step. Executors perform
those steps and never compile a skill themselves. Secrets stay handles until dispatch
(INV-5); a skill whose steps include a destructive one makes its plan step destructive, so
the human approval in INV-7 is asked before anything runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from slas_kernel.clock import Clock
from slas_schemas.common import AgentName
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Plan, Step
from slas_skills.compiler import CompiledSkill, SkillCompileError, compile_skill
from slas_skills.primitives import RISK_ORDER, max_risk
from slas_skills.schema import Skill
from slas_skills.state import AGENT_LABEL, SkillStateStore

SKILL_PRIMITIVE: Final = "skill"
#: The plan-step arguments a skill may take its target from, in order of preference.
TARGET_ARGS: Final[tuple[str, ...]] = ("station", "target")
#: What the compiler is given for a secret input so that it hands out a handle (INV-5).
SECRET_PLACEHOLDER: Final = "resolved-at-dispatch"  # noqa: S105 — a marker, not a secret


class SkillGateError(ValueError):
    def __init__(self, step: Step, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.step = step
        self.message = message


def compiled_from_step(step: Step) -> CompiledSkill | None:
    """The compiled skill the gate stored on a plan step, or None if the gate never ran."""
    raw = step.args.get("compiled")
    if not isinstance(raw, Mapping):
        return None
    return CompiledSkill.model_validate(raw)


class SkillGate:
    def __init__(
        self,
        *,
        library: Mapping[str, Skill],
        state: SkillStateStore,
        clock: Clock,
    ) -> None:
        self.library = dict(library)
        self.state = state
        self.clock = clock

    def expand(self, plan: Plan, *, agent: AgentName) -> Plan:
        """The same plan with every `skill` step carrying its compiled steps and true risk."""
        steps = [
            self._expand_step(step, plan.job_id, agent)
            if step.primitive == SKILL_PRIMITIVE
            else step
            for step in plan.steps
        ]
        if steps == plan.steps:
            return plan
        return plan.model_copy(update={"steps": steps})

    def _expand_step(self, step: Step, job_id: str, agent: AgentName) -> Step:
        skill_id = str(step.args.get("skill_id", ""))
        who = AGENT_LABEL.get(agent, agent)
        skill = self.library.get(skill_id)
        if skill is None:
            raise SkillGateError(
                step,
                ThreePartMessage(
                    f"The skill {skill_id or '(unnamed)'} is not in this installation's library.",
                    "The plan names a skill nobody has imported here, or it was deleted.",
                    "Import it under Skills, or remove the step from the plan.",
                ),
            )
        if agent not in skill.agents:
            allowed = ", ".join(AGENT_LABEL.get(a, a) for a in skill.agents)
            raise SkillGateError(
                step,
                ThreePartMessage(
                    f"{skill.name} does not work with {who}.",
                    f"Its author allows only {allowed}.",
                    "Remove the step, or ask the author for a version that lists this agent.",
                ),
            )
        if not self.state.is_enabled(skill.id, agent):
            raise SkillGateError(
                step,
                ThreePartMessage(
                    f"{skill.name} is not turned on for {who}.",
                    "Someone turned it off, or it was never turned on here.",
                    "Turn it on under Skills, or remove it from the plan.",
                ),
            )
        inputs: dict[str, Any] = dict(step.args.get("inputs", {}))
        for name in TARGET_ARGS:
            if name in step.args and name in skill.inputs and name not in inputs:
                inputs[name] = step.args[name]
        for name in dict(step.args.get("secret_refs", {})):
            inputs[name] = SECRET_PLACEHOLDER
        try:
            compiled = compile_skill(skill, inputs, job_id=job_id, now=self.clock.now())
        except SkillCompileError as exc:
            raise SkillGateError(step, exc.message) from exc
        risk = max_risk([s.risk for s in compiled.plan.steps])
        if RISK_ORDER[risk] < RISK_ORDER[step.risk]:
            risk = step.risk
        return step.model_copy(
            update={
                "args": {**step.args, "compiled": compiled.model_dump(mode="json")},
                "risk": risk,
            }
        )
