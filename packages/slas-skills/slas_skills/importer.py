"""IMPORT: schema → primitive whitelist → capability check → risk classification (§5.6).

Skills never grant capabilities; they consume the importing user's. A destructive step
shows its approval requirement at import time, and again at every run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pydantic import Field

from slas_authz import Capability, Principal
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_skills.primitives import PRIMITIVES, Risk, max_risk, risk_of
from slas_skills.schema import Skill, SkillImportError, SkillStep, parse_skill
from slas_skills.yamlout import render_skill_yaml, skill_to_mapping

#: Capabilities every run has without declaring them: the job's own workspace.
IMPLICIT_CAPABILITIES: frozenset[Capability] = frozenset({Capability.FILES})


def walk(steps: list[SkillStep], path: str = "steps") -> Iterator[tuple[str, SkillStep]]:
    for index, step in enumerate(steps):
        here = f"{path}[{index}]"
        yield here, step
        yield from walk(step.then, f"{here}.then")
        yield from walk(step.otherwise, f"{here}.else")


class ImportReport(SlasModel):
    skill: Skill
    risk: Risk
    destructive_steps: list[str] = Field(default_factory=list)
    sentence: str
    approval_sentence: str | None = None

    @property
    def needs_approval(self) -> bool:
        return self.risk == "destructive"


def _describe_destructive(step: SkillStep) -> str:
    target = step.args.get("target", "the target")
    return f"{step.primitive} {step.args.get('action', '')} on {target}".strip()


def import_skill(
    data: object,
    *,
    principal: Principal | None = None,
    source: str = "<memory>",
) -> ImportReport:
    skill = parse_skill(data, source=source)
    # `files` is the job's own workspace, which every run has (CLAUDE.md §5.2), so a skill
    # need not declare it; the §6.3 example copies a file while requiring only redfish.
    required = set(skill.requires) | IMPLICIT_CAPABILITIES

    # Every other primitive need must be covered by what the skill declares it requires.
    for path, step in walk(skill.steps):
        spec = PRIMITIVES[step.primitive]
        if spec.needs and not any(need in required for need in spec.needs):
            options = " or ".join(need.value for need in spec.needs)
            raise SkillImportError(
                ThreePartMessage(
                    f"{source} uses `{step.primitive}` at {path} but does not require {options}.",
                    "A skill declares under `requires:` every capability its steps need, so that "
                    "the capability check happens before anything runs.",
                    f"Add {options.split(' or ')[0]} to `requires:` or remove the step.",
                )
            )

    # The importing user must hold everything the skill requires (INV-12).
    if principal is not None:
        missing = [c for c in skill.requires if not principal.can(c)]
        if missing:
            names = ", ".join(c.value for c in missing)
            raise SkillImportError(
                ThreePartMessage(
                    f"You can't import {skill.name}: it needs {names}.",
                    f"Your {principal.role_label} role does not include "
                    f"{'it' if len(missing) == 1 else 'them'}, and a skill never adds "
                    "capabilities.",
                    "Ask an administrator for a role that includes it, or import a skill that "
                    "needs less.",
                )
            )

    risks = [risk_of(step.primitive, step.args) for _, step in walk(skill.steps)]
    destructive = [
        _describe_destructive(step)
        for _, step in walk(skill.steps)
        if risk_of(step.primitive, step.args) == "destructive"
    ]
    risk = max_risk(risks)
    count = sum(1 for _ in walk(skill.steps))
    noun = "step" if count == 1 else "steps"
    requires = ", ".join(c.value for c in skill.requires) or "no capabilities"
    sentence = f"{skill.name} v{skill.version}: {count} {noun}, needs {requires}, risk {risk}."
    approval: str | None = None
    if destructive:
        approval = (
            f"This skill contains {len(destructive)} destructive "
            f"{'step' if len(destructive) == 1 else 'steps'} ({'; '.join(destructive)}) and will "
            "ask for your approval every time it runs."
        )
    return ImportReport(
        skill=skill,
        risk=risk,
        destructive_steps=destructive,
        sentence=sentence,
        approval_sentence=approval,
    )


def store_skill(report: ImportReport, library_dir: Path) -> Path:
    """Write the imported skill under Skills/library/<id>.skill.yaml."""
    library_dir.mkdir(parents=True, exist_ok=True)
    path = library_dir / f"{report.skill.id}.skill.yaml"
    write_atomic(path, render_skill_yaml(skill_to_mapping(report.skill)), mode=0o644)
    return path


def capabilities_sentence(capabilities: list[Capability]) -> str:
    if not capabilities:
        return "This skill needs no capabilities."
    names = ", ".join(c.value for c in capabilities)
    return f"Whoever runs this skill must hold {names}."
