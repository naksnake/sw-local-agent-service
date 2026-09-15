"""Per-agent enablement: an installation's record, kept beside the skill file (ADR-0013).

`Skills/library/<id>.state.json` says which agents a skill is turned on for *here*. The
skill file itself never carries it, so a skill travels between sites byte-identical and
arrives off. Off is the default; a switch exists only for agents the author listed; turning
a skill on grants nothing (INV-12) and is never an approval (INV-7). The record is read on
use, so a toggle needs no restart (INV-9).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_skills.importer import walk
from slas_skills.primitives import RISK_ORDER, Risk, max_risk, risk_of
from slas_skills.schema import Skill, SkillAgent

AGENT_LABEL: Final[dict[str, str]] = {
    "coding": "the Coding Agent",
    "validation": "the Validation Agent",
    "factory": "the Factory Agent",
}
PLATFORM: Final = "platform"


class SkillStateError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class EnabledMark(SlasModel):
    by: str = Field(min_length=1)
    at: datetime


class SkillState(SlasModel):
    skill_id: str
    version: str
    imported_by: str = Field(min_length=1)
    imported_at: datetime
    #: What the recorded version allowed and needed; a re-import that grows resets `enabled`.
    agents: list[SkillAgent]
    requires: list[str] = Field(default_factory=list)
    risk: Risk = "safe"
    enabled: dict[SkillAgent, EnabledMark] = Field(default_factory=dict)
    #: Why the last re-import turned everything off, for the import result and the page.
    reset_reason: str | None = None

    def is_enabled(self, agent: str) -> bool:
        return agent in self.enabled

    def sentence(self) -> str:
        if not self.enabled:
            return "Off for every agent."
        parts = [
            f"{AGENT_LABEL.get(agent, agent)} since {mark.at:%d %B %Y}"
            for agent, mark in sorted(self.enabled.items())
        ]
        return "On for " + "; ".join(parts) + "."


def skill_risk(skill: Skill) -> Risk:
    return max_risk([risk_of(step.primitive, step.args) for _, step in walk(skill.steps)])


class SkillStateStore:
    """The `<id>.state.json` files under one library directory; the api is the only writer."""

    def __init__(self, library_dir: Path) -> None:
        self.library_dir = library_dir

    def path(self, skill_id: str) -> Path:
        return self.library_dir / f"{skill_id}.state.json"

    def load(self, skill_id: str) -> SkillState | None:
        path = self.path(skill_id)
        if not path.is_file():
            return None
        return SkillState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, state: SkillState) -> Path:
        path = self.path(state.skill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, state.model_dump_json(indent=2) + "\n", mode=0o644)
        return path

    def is_enabled(self, skill_id: str, agent: str) -> bool:
        state = self.load(skill_id)
        return state is not None and state.is_enabled(agent)

    def record_import(self, skill: Skill, *, by: str, now: datetime) -> SkillState:
        """A fresh import is off everywhere; a replacement keeps its switches only if the new
        version allows no more agents, needs no more capabilities and is no riskier."""
        previous = self.load(skill.id)
        fresh = SkillState(
            skill_id=skill.id,
            version=skill.version,
            imported_by=by,
            imported_at=now,
            agents=list(skill.agents),
            requires=[c.value for c in skill.requires],
            risk=skill_risk(skill),
        )
        if previous is None:
            self.save(fresh)
            return fresh
        grew: list[str] = []
        if not set(skill.agents) <= set(previous.agents):
            grew.append("works with more agents")
        if not set(fresh.requires) <= set(previous.requires):
            grew.append("needs more capabilities")
        if RISK_ORDER[fresh.risk] > RISK_ORDER[previous.risk]:
            grew.append(f"is riskier ({previous.risk} → {fresh.risk})")
        if grew:
            fresh.reset_reason = (
                f"Version {skill.version} {', '.join(grew)} compared with "
                f"{previous.version}, so it starts off for every agent."
            )
        else:
            fresh.enabled = dict(previous.enabled)
        self.save(fresh)
        return fresh

    def enable(self, skill: Skill, agent: SkillAgent, *, by: str, now: datetime) -> SkillState:
        state = self._existing(skill.id)
        if agent not in skill.agents:
            allowed = ", ".join(AGENT_LABEL.get(a, a) for a in skill.agents)
            raise SkillStateError(
                ThreePartMessage(
                    f"{skill.name} can't be turned on for {AGENT_LABEL.get(agent, agent)}.",
                    f"Its author allows only {allowed}.",
                    "Ask the author for a version that lists that agent, or use it where it "
                    "is allowed.",
                )
            )
        state.enabled[agent] = EnabledMark(by=by, at=now)
        state.reset_reason = None
        self.save(state)
        return state

    def disable(self, skill_id: str, agent: SkillAgent) -> SkillState:
        state = self._existing(skill_id)
        state.enabled.pop(agent, None)
        self.save(state)
        return state

    def _existing(self, skill_id: str) -> SkillState:
        state = self.load(skill_id)
        if state is None:
            raise SkillStateError(
                ThreePartMessage(
                    f"There is no record for the skill {skill_id} on this installation.",
                    "It was never imported here, or its record was removed by hand.",
                    "Import the skill under Skills; the record is created with it.",
                )
            )
        return state
