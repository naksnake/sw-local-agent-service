"""The skill recipe model (CLAUDE.md §6.1) and how a parsed skill file becomes one.

A skill file is data (INV-12): it is validated against this model and the primitive
whitelist, and every problem is reported as a three-part sentence naming the step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

from slas_authz import SKILL_REQUIRES, Capability, parse_capability
from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_skills.primitives import (
    MAX_WAIT_SECONDS,
    PRIMITIVES,
    REDFISH_ACTIONS,
    Primitive,
)

InputType = Literal["string", "int", "bool", "path", "target_ref", "secret"]
SkillAgent = Literal["coding", "validation", "factory"]
SEMVER = r"^\d+\.\d+\.\d+$"
SLUG = r"^[a-z][a-z0-9_-]*$"


class SkillImportError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class InputSpec(SlasModel):
    type: InputType
    required: bool = False
    default: Any = None
    description: str = ""


class RetrySpec(SlasModel):
    max: int = Field(ge=1, le=10)
    delay_s: int = Field(default=0, ge=0, le=600)


class RetryFailure(SlasModel):
    retry: RetrySpec


OnFailure = Literal["stop", "screenshot_and_stop", "continue"] | RetryFailure


class OutputSpec(SlasModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source: str = Field(alias="from", min_length=1)


class SopSummary(SlasModel):
    en: str = Field(min_length=1)
    zh: str = Field(min_length=1)


class SkillStep(SlasModel):
    primitive: str
    args: dict[str, Any] = Field(default_factory=dict)
    id: str | None = Field(default=None, pattern=SLUG)
    when: str | None = None
    then: list[SkillStep] = Field(default_factory=list)
    otherwise: list[SkillStep] = Field(default_factory=list)

    @property
    def spec(self) -> Primitive:
        return PRIMITIVES[self.primitive]


class Skill(SlasModel):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(pattern=SEMVER)
    description: str = ""
    agents: list[SkillAgent] = Field(min_length=1)
    requires: list[Capability] = Field(default_factory=list)
    timeout_s: int = Field(default=900, ge=1, le=86400)
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    steps: list[SkillStep] = Field(min_length=1)
    on_failure: OnFailure = "stop"
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    sop_summary: SopSummary | None = None

    @field_validator("requires")
    @classmethod
    def _only_skill_capabilities(cls, value: list[Capability]) -> list[Capability]:
        bad = [c.value for c in value if c not in SKILL_REQUIRES]
        if bad:
            allowed = ", ".join(sorted(c.value for c in SKILL_REQUIRES))
            raise ValueError(f"requires may only list {allowed}; not {', '.join(bad)}")
        if len(set(value)) != len(value):
            raise ValueError("requires lists a capability twice")
        return value

    @field_validator("agents")
    @classmethod
    def _distinct_agents(cls, value: list[SkillAgent]) -> list[SkillAgent]:
        if len(set(value)) != len(value):
            raise ValueError("agents lists an agent twice")
        return value

    def step_ids(self) -> list[str]:
        ids: list[str] = []

        def walk(steps: list[SkillStep]) -> None:
            for step in steps:
                if step.id:
                    ids.append(step.id)
                walk(step.then)
                walk(step.otherwise)

        walk(self.steps)
        return ids


# --- parsing -----------------------------------------------------------------------------


def _error(source: str, what: str, cause: str, todo: str | None = None) -> SkillImportError:
    return SkillImportError(
        ThreePartMessage(
            what,
            cause,
            todo or f"Fix {source} and import it again; the whitelist is CLAUDE.md §6.2.",
        )
    )


def parse_step(raw: object, *, source: str, path: str) -> SkillStep:
    if not isinstance(raw, Mapping):
        raise _error(
            source,
            f"Step {path} in {source} is not a mapping.",
            "Each step is `- <primitive>: { <args> }`, optionally with `id:` and `when:`.",
        )
    keys = [str(key) for key in raw]
    meta = {"id", "when"}
    verbs = [key for key in keys if key not in meta]
    if len(verbs) != 1:
        raise _error(
            source,
            f"Step {path} in {source} must name exactly one primitive; it names "
            f"{', '.join(verbs) or 'none'}.",
            "A step is one verb with its arguments, plus optional `id` and `when`.",
        )
    verb = verbs[0]
    spec = PRIMITIVES.get(verb)
    if spec is None:
        raise _error(
            source,
            f"Step {path} in {source} uses `{verb}`, which is not a skill primitive.",
            "Skills may only use the whitelisted verbs; there is no shell, eval, python, "
            "download or sudo.",
        )
    raw_args = raw[verb]
    if not isinstance(raw_args, Mapping):
        raise _error(
            source,
            f"Step {path} ({verb}) in {source} has no argument mapping.",
            f"Write `{verb}: {{ … }}` with its arguments.",
        )
    args = {str(key): value for key, value in raw_args.items()}
    _check_args(spec, args, source=source, path=path)
    then_steps: list[SkillStep] = []
    else_steps: list[SkillStep] = []
    if spec.control:
        then_steps = _parse_steps(args.pop("then"), source=source, path=f"{path}.then")
        if "else" in args:
            else_steps = _parse_steps(args.pop("else"), source=source, path=f"{path}.else")
    return SkillStep(
        primitive=verb,
        args=args,
        id=str(raw["id"]) if "id" in raw and raw["id"] is not None else None,
        when=str(raw["when"]) if "when" in raw and raw["when"] is not None else None,
        then=then_steps,
        otherwise=else_steps,
    )


def _parse_steps(raw: object, *, source: str, path: str) -> list[SkillStep]:
    if not isinstance(raw, list) or not raw:
        raise _error(
            source,
            f"{path} in {source} must be a non-empty list of steps.",
            "Control steps carry their branches as lists of steps.",
        )
    return [parse_step(item, source=source, path=f"{path}[{i}]") for i, item in enumerate(raw)]


def _check_args(spec: Primitive, args: Mapping[str, Any], *, source: str, path: str) -> None:
    label = f"Step {path} ({spec.name}) in {source}"
    unknown = sorted(set(args) - spec.known_args())
    if unknown:
        raise _error(
            source,
            f"{label} has arguments it does not take: {', '.join(unknown)}.",
            f"`{spec.name}` takes {', '.join(sorted(spec.known_args())) or 'no arguments'}.",
        )
    missing = [name for name in spec.required if name not in args]
    if missing:
        raise _error(
            source,
            f"{label} is missing {', '.join(missing)}.",
            f"`{spec.name}` needs {', '.join(spec.required)}.",
        )
    if spec.one_of and not any(all(name in args for name in group) for group in spec.one_of):
        groups = " or ".join("+".join(group) for group in spec.one_of)
        raise _error(source, f"{label} needs a target.", f"Give one of {groups}.")
    if spec.name == "redfish" and str(args.get("action")) not in REDFISH_ACTIONS:
        raise _error(
            source,
            f"{label} uses the unknown action {args.get('action')!r}.",
            f"Redfish actions are {', '.join(sorted(REDFISH_ACTIONS))}.",
        )
    if spec.name == "wait":
        seconds = args.get("seconds")
        if isinstance(seconds, int | float) and not 0 <= seconds <= MAX_WAIT_SECONDS:
            raise _error(
                source,
                f"{label} waits {seconds} seconds.",
                f"A wait is at most {MAX_WAIT_SECONDS} seconds.",
            )
    command = args.get("command")
    if command is not None and not (
        isinstance(command, list) and all(isinstance(x, str) for x in command)
    ):
        raise _error(
            source,
            f"{label} has a `command` that is not an argv list.",
            "Commands are lists of strings; they are never joined into a shell line.",
        )


def parse_skill(data: object, *, source: str = "<memory>") -> Skill:
    if not isinstance(data, Mapping) or not isinstance(data.get("skill"), Mapping):
        raise _error(
            source,
            f"{source} is not a skill file.",
            "A skill file has one top-level `skill:` mapping (CLAUDE.md §6.1).",
        )
    body: dict[str, Any] = dict(data["skill"])
    raw_steps = body.pop("steps", None)
    steps = _parse_steps(raw_steps, source=source, path="steps")
    requires = body.pop("requires", [])
    if not isinstance(requires, list):
        raise _error(
            source,
            f"`requires` in {source} must be a list.",
            "List the capabilities the user must hold, e.g. [screen, ssh].",
        )
    capabilities: list[Capability] = []
    for name in requires:
        capability = parse_capability(str(name))
        if capability is None:
            raise _error(
                source,
                f"`requires` in {source} names an unknown capability {name!r}.",
                f"Capabilities are {', '.join(sorted(c.value for c in SKILL_REQUIRES))}.",
            )
        capabilities.append(capability)
    try:
        return Skill.model_validate({**body, "steps": steps, "requires": capabilities})
    except ValidationError as exc:
        raise _error(source, f"{source} could not be imported.", validation_sentence(exc)) from exc
