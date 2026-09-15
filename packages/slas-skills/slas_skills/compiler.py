"""COMPILE: bind inputs, hand out secret handles, unroll bounded loops → a flat StepPlan (§5.6).

The result is a `slas_schemas.plan.Plan` whose steps the executor understands; it is the
same for every agent. Secrets never appear in the plan: an input of type `secret` is bound
to an opaque handle that the executor resolves at dispatch (INV-5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import MAX_STEPS, Plan, Step
from slas_skills.primitives import MAX_FOREACH_ITEMS, risk_of
from slas_skills.schema import Skill, SkillStep
from slas_skills.templates import (
    TemplateError,
    evaluate,
    has_late_reference,
    references,
    render_value,
)

SECRET_PREFIX = "secret://"  # noqa: S105 — a handle scheme the executor resolves, not a secret


class SkillCompileError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class CompiledSkill(SlasModel):
    skill_id: str
    plan: Plan
    #: Step ids whose arguments carry a secret handle; the runner never logs their arguments.
    secret_steps: list[str] = Field(default_factory=list)
    #: Input name → secret handle, for the executor to resolve at dispatch.
    secret_handles: dict[str, str] = Field(default_factory=dict)
    #: Output name → step id (from `outputs: {name: {from: step}}`).
    outputs: dict[str, str] = Field(default_factory=dict)
    on_failure: Any = "stop"
    timeout_s: int = 900

    def sentence(self) -> str:
        return self.plan.sentence()


def _coerce(name: str, kind: str, value: Any, skill: Skill) -> Any:
    try:
        if kind == "int" and not isinstance(value, bool):
            return int(value)
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "1", "on")
            return bool(value)
    except (TypeError, ValueError) as exc:
        raise SkillCompileError(
            ThreePartMessage(
                f"The input {name} of {skill.name} must be a {kind}; got {value!r}.",
                f"The skill declares `{name}` as {kind}.",
                "Give a value of that type.",
            )
        ) from exc
    return value


def bind_inputs(skill: Skill, given: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Inputs → context. Secrets become handles; required ones must be present."""
    unknown = sorted(set(given) - set(skill.inputs))
    if unknown:
        raise SkillCompileError(
            ThreePartMessage(
                f"{skill.name} was given inputs it does not declare: {', '.join(unknown)}.",
                f"Its inputs are {', '.join(skill.inputs) or 'none'}.",
                "Remove the extra inputs or declare them in the skill.",
            )
        )
    context: dict[str, Any] = {}
    handles: dict[str, str] = {}
    missing: list[str] = []
    for name, spec in skill.inputs.items():
        present = name in given and given[name] is not None
        if spec.type == "secret":
            if present or spec.required:
                if not present:
                    missing.append(name)
                    continue
                handle = f"{SECRET_PREFIX}{skill.id}/{name}"
                handles[name] = handle
                context[name] = handle
            continue
        if present:
            context[name] = _coerce(name, spec.type, given[name], skill)
        elif spec.default is not None:
            context[name] = spec.default
        elif spec.required:
            missing.append(name)
    if missing:
        noun = "input" if len(missing) == 1 else "inputs"
        raise SkillCompileError(
            ThreePartMessage(
                f"{skill.name} needs the {noun} {', '.join(missing)}.",
                "They are declared as required and have no default.",
                "Provide them when starting the skill.",
            )
        )
    return context, handles


class _Expander:
    def __init__(self, skill: Skill, context: dict[str, Any], handles: dict[str, str]) -> None:
        self.skill = skill
        self.context = context
        self.handle_values = set(handles.values())
        self.steps: list[Step] = []
        self.secret_steps: list[str] = []
        self._used_ids: set[str] = set()

    def _unique_id(self, wanted: str | None, primitive: str) -> str:
        base = wanted or f"{self.skill.id}-{primitive.replace('_', '-')}"
        candidate = base
        counter = 2
        while candidate in self._used_ids:
            candidate = f"{base}-{counter}"
            counter += 1
        self._used_ids.add(candidate)
        return candidate

    def _carries_secret(self, value: Any) -> bool:
        if isinstance(value, str):
            return any(handle in value for handle in self.handle_values)
        if isinstance(value, list):
            return any(self._carries_secret(v) for v in value)
        if isinstance(value, Mapping):
            return any(self._carries_secret(v) for v in value.values())
        return False

    def expand(self, steps: list[SkillStep], when: str | None = None) -> None:
        for step in steps:
            if step.primitive == "if":
                self._expand_if(step, when)
            elif step.primitive == "foreach":
                self._expand_foreach(step, when)
            else:
                self._emit(step, when)
            if len(self.steps) > MAX_STEPS:
                raise SkillCompileError(
                    ThreePartMessage(
                        f"{self.skill.name} expands to more than {MAX_STEPS} steps.",
                        "Loops are unrolled at compile time and the plan is capped "
                        "(CLAUDE.md §6.1).",
                        "Shorten the loops or split the skill.",
                    )
                )

    def _combine(self, outer: str | None, inner: str | None) -> str | None:
        if outer and inner:
            return f"({outer}) and ({inner})"
        return outer or inner

    def _emit(self, step: SkillStep, when: str | None) -> None:
        source = f"{self.skill.name} step {step.id or step.primitive}"
        try:
            args = render_value(step.args, self.context, source=source)
        except TemplateError as exc:
            raise SkillCompileError(exc.message) from exc
        step_when = self._combine(when, step.when)
        if step_when and not has_late_reference(step_when) and not references(step_when):
            pass  # a literal condition is kept for the runner; it is cheap to evaluate
        risk = risk_of(step.primitive, args)
        step_id = self._unique_id(step.id, step.primitive)
        self.steps.append(
            Step(
                id=step_id,
                n=len(self.steps) + 1,
                primitive=step.primitive,
                title=_title(step.primitive, args),
                args=args,
                risk=risk,
                when=step_when,
            )
        )
        if self._carries_secret(args):
            self.secret_steps.append(step_id)

    def _expand_if(self, step: SkillStep, when: str | None) -> None:
        condition = str(step.args["condition"])
        source = f"{self.skill.name} step {step.id or 'if'}"
        if has_late_reference(condition):
            # Depends on a step output: decide at run time, on both branches.
            self.expand(step.then, self._combine(when, condition))
            if step.otherwise:
                self.expand(step.otherwise, self._combine(when, f"not {condition}"))
            return
        try:
            holds = evaluate(condition, self.context, source=source)
        except TemplateError as exc:
            raise SkillCompileError(exc.message) from exc
        self.expand(step.then if holds else step.otherwise, when)

    def _expand_foreach(self, step: SkillStep, when: str | None) -> None:
        source = f"{self.skill.name} step {step.id or 'foreach'}"
        try:
            items = render_value(step.args["items"], self.context, source=source)
        except TemplateError as exc:
            raise SkillCompileError(exc.message) from exc
        if isinstance(items, str):
            items = [item.strip() for item in items.split(",") if item.strip()]
        if not isinstance(items, list):
            raise SkillCompileError(
                ThreePartMessage(
                    f"{source} loops over something that is not a list.",
                    "`items` must be a list, or a comma-separated string input.",
                    "Fix the items.",
                )
            )
        if len(items) > MAX_FOREACH_ITEMS:
            raise SkillCompileError(
                ThreePartMessage(
                    f"{source} loops over {len(items)} items; the limit is {MAX_FOREACH_ITEMS}.",
                    "Loops are bounded so a plan stays reviewable (CLAUDE.md §6.2).",
                    "Split the work into several runs.",
                )
            )
        var = str(step.args.get("as", "item"))
        saved = self.context.get(var)
        for index, item in enumerate(items):
            self.context[var] = item
            self.context[f"{var}_index"] = index
            self.expand(step.then, when)
        self.context.pop(f"{var}_index", None)
        if saved is None:
            self.context.pop(var, None)
        else:
            self.context[var] = saved


def _title(primitive: str, args: Mapping[str, Any]) -> str:
    if primitive == "redfish":
        return f"Redfish {args.get('action')} on {args.get('target')}"
    if primitive in ("click", "double_click", "right_click"):
        target = args.get("text") or args.get("target") or args.get("image") or "coordinates"
        return f"{primitive.replace('_', ' ').capitalize()} {target}"
    if primitive == "focus_window":
        return f"Focus window {args.get('title') or args.get('class')}"
    if primitive == "type":
        return "Type text"
    if primitive == "key":
        return f"Press {args.get('press')}"
    if primitive in ("run", "ssh"):
        command = args.get("command")
        shown = " ".join(str(c) for c in command[:3]) if isinstance(command, list) else "command"
        return f"{'Run' if primitive == 'run' else 'SSH'} {shown}"
    if primitive == "wait_for":
        return f"Wait for {args.get('window') or args.get('text') or args.get('image')}"
    if primitive == "assert":
        return f"Check {args.get('message')}"
    return primitive.replace("_", " ").capitalize()


def compile_skill(
    skill: Skill,
    inputs: Mapping[str, Any],
    *,
    job_id: str,
    now: datetime,
) -> CompiledSkill:
    context, handles = bind_inputs(skill, inputs)
    expander = _Expander(skill, context, handles)
    expander.expand(skill.steps)
    known_ids = {step.id for step in expander.steps}
    for name, spec in skill.outputs.items():
        if spec.source not in known_ids:
            raise SkillCompileError(
                ThreePartMessage(
                    f"The output {name} of {skill.name} comes from step {spec.source}, "
                    "which does not exist.",
                    "Outputs point at a step by its `id`.",
                    "Give the step that id, or fix the output.",
                )
            )
    digest = hashlib.sha256(
        json.dumps(
            {"skill": skill.id, "version": skill.version, "inputs": sorted(context)},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    plan = Plan(
        id=f"skill-{skill.id}-{digest}",
        job_id=job_id,
        summary=f"{skill.name} v{skill.version}",
        steps=expander.steps,
        created_at=now,
    )
    return CompiledSkill(
        skill_id=skill.id,
        plan=plan,
        secret_steps=expander.secret_steps,
        secret_handles=handles,
        outputs={name: spec.source for name, spec in skill.outputs.items()},
        on_failure=skill.on_failure
        if isinstance(skill.on_failure, str)
        else skill.on_failure.model_dump(),
        timeout_s=skill.timeout_s,
    )
