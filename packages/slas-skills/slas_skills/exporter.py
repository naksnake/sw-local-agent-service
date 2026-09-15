"""EXPORT: the stored YAML with secrets stripped and a content hash (§5.6).

The hash is over the canonical JSON of the skill, so it is the same on every installation
that imports the file unchanged; the YAML carries it in its header comment.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_skills.schema import Skill
from slas_skills.yamlout import render_skill_yaml, skill_to_mapping


class ExportedSkill(SlasModel):
    skill_id: str
    yaml: str
    canonical_json: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def sentence(self) -> str:
        return f"Exported {self.skill_id} ({self.sha256[:12]}); secrets were stripped."


def content_hash(skill: Skill) -> tuple[str, str]:
    mapping = skill_to_mapping(skill, strip_secret_defaults=True)
    canonical = json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def export_skill(skill: Skill) -> ExportedSkill:
    canonical, digest = content_hash(skill)
    header = (
        f"{skill.name} — skill {skill.id} v{skill.version} for SW Local Agent Service\n"
        f"sha256: {digest}\n"
        "Secrets are never stored in a skill file; inputs of type secret are asked for at run time."
    )
    yaml = render_skill_yaml(skill_to_mapping(skill, strip_secret_defaults=True), header=header)
    return ExportedSkill(skill_id=skill.id, yaml=yaml, canonical_json=canonical, sha256=digest)
