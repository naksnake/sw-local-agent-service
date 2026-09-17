"""`/v1/skills`: the library on disk, import, per-agent toggles, export (contract §5, §5.6).

Skills are data (INV-12): a file is parsed with `yaml.safe_load`, validated by
`slas_skills.importer`, stored under `Skills/library/<id>.skill.yaml`; its enablement lives
beside it in `<id>.state.json` (ADR-0013). The acting person comes from the identity
headers; the import checks that person's capabilities and never grants any.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from fastapi import APIRouter, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from slas_authz import Capability, Principal, parse_capability
from slas_http.errors import ServiceError
from slas_http.identity import Identity, identity_of
from slas_orchestrator.service.deps import Deps, deps_of, problem
from slas_schemas.errors import ThreePartMessage
from slas_skills.exporter import export_skill
from slas_skills.importer import import_skill, store_skill
from slas_skills.schema import Skill, SkillAgent, SkillImportError, parse_skill
from slas_skills.state import SkillStateError, SkillStateStore, skill_risk

router = APIRouter(prefix="/v1/skills")

SKILL_SUFFIX: Final = ".skill.yaml"
MAX_SKILL_BYTES: Final = 256 * 1024


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(min_length=1, max_length=MAX_SKILL_BYTES)


class ToggleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: SkillAgent


# --- the library on disk ---------------------------------------------------------------------


def load_library(library_dir: Path, *, log_skip: Any = None) -> dict[str, Skill]:
    """Every `<id>.skill.yaml` in the directory that parses; the rest is reported, not fatal."""
    library: dict[str, Skill] = {}
    if not library_dir.is_dir():
        return library
    for path in sorted(library_dir.glob(f"*{SKILL_SUFFIX}")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            skill = parse_skill(data, source=path.name)
        except (OSError, yaml.YAMLError, SkillImportError) as exc:
            if log_skip is not None:
                log_skip(path.name, str(exc))
            continue
        library[skill.id] = skill
    return library


def skill_row(skill: Skill, state_store: SkillStateStore) -> dict[str, Any]:
    state = state_store.load(skill.id)
    enabled = {agent: state is not None and state.is_enabled(agent) for agent in skill.agents}
    return {
        "id": skill.id,
        "name": skill.name,
        "version": skill.version,
        "risk": state.risk if state is not None else skill_risk(skill),
        "agents": list(skill.agents),
        "requires": [c.value for c in skill.requires],
        "enabled": enabled,
        "sentence": state.sentence() if state is not None else "Off for every agent.",
    }


def enabled_for(library: dict[str, Skill], state_store: SkillStateStore, agent: str) -> list[Skill]:
    return [
        s for s in library.values() if agent in s.agents and state_store.is_enabled(s.id, agent)
    ]


def principal_of(identity: Identity) -> Principal:
    """The importing person as `slas_authz` sees them: exactly the capabilities forwarded."""
    capabilities: set[Capability] = set()
    for name in identity.capabilities:
        capability = parse_capability(name)
        if capability is not None:
            capabilities.add(capability)
    return Principal(
        subject=identity.user,
        display_name=identity.name,
        role="",
        role_label="current",
        capabilities=frozenset(capabilities),
    )


def _library(deps: Deps) -> dict[str, Skill]:
    return load_library(
        deps.settings.skills_library,
        log_skip=lambda name, why: deps.log.warning("skill.unreadable", file=name, why=why),
    )


def _skill(deps: Deps, skill_id: str) -> Skill:
    skill = _library(deps).get(skill_id)
    if skill is None:
        raise ServiceError(
            404,
            ThreePartMessage(
                f"There is no skill {skill_id} in this installation's library.",
                "It was never imported here, or it was removed.",
                "Import it under Skills, or pick one from the list.",
            ),
        )
    return skill


# --- routes -------------------------------------------------------------------------------


@router.get("")
def list_skills(request: Request) -> list[dict[str, Any]]:
    identity_of(request)
    deps = deps_of(request)
    return [skill_row(skill, deps.skill_state) for skill in _library(deps).values()]


@router.post("/import")
def import_route(request: Request, body: ImportBody) -> dict[str, Any]:
    identity = identity_of(request)
    deps = deps_of(request)
    try:
        data = yaml.safe_load(body.yaml)
    except yaml.YAMLError as exc:
        raise ServiceError(
            400,
            ThreePartMessage(
                "The skill file is not valid YAML.",
                str(exc).splitlines()[0] if str(exc) else "The parser found a syntax error.",
                "Fix the file and import it again; the format is CLAUDE.md §6.1.",
            ),
        ) from exc
    try:
        report = import_skill(data, principal=principal_of(identity), source="the skill file")
    except SkillImportError as exc:
        raise ServiceError(400, exc.message) from exc
    store_skill(report, deps.settings.skills_library)
    state = deps.skill_state.record_import(report.skill, by=identity.user, now=deps.clock.now())
    deps.log.info("skill.imported", skill_id=report.skill.id, version=report.skill.version)
    answer = report.model_dump(mode="json", by_alias=True)
    answer["state_sentence"] = state.reset_reason or state.sentence()
    return answer


def _toggle(request: Request, skill_id: str, body: ToggleBody, *, on: bool) -> dict[str, Any]:
    identity = identity_of(request)
    deps = deps_of(request)
    skill = _skill(deps, skill_id)
    try:
        if on:
            deps.skill_state.enable(skill, body.agent, by=identity.user, now=deps.clock.now())
        else:
            deps.skill_state.disable(skill.id, body.agent)
    except SkillStateError as exc:
        fallback = ThreePartMessage(
            f"{skill.name} could not be switched.",
            "Its record on this installation could not be changed.",
            "Import the skill again, then try the switch.",
        )
        status = 404 if "no record" in exc.message.what_happened else 400
        raise problem(status, exc, fallback=fallback) from exc
    deps.log.info("skill.toggled", skill_id=skill.id, agent=body.agent, on=on)
    return skill_row(skill, deps.skill_state)


@router.post("/{id}/enable")
def enable_route(
    request: Request, body: ToggleBody, skill_id: str = PathParam(alias="id")
) -> dict[str, Any]:
    return _toggle(request, skill_id, body, on=True)


@router.post("/{id}/disable")
def disable_route(
    request: Request, body: ToggleBody, skill_id: str = PathParam(alias="id")
) -> dict[str, Any]:
    return _toggle(request, skill_id, body, on=False)


@router.get("/{id}/export")
def export_route(request: Request, skill_id: str = PathParam(alias="id")) -> dict[str, Any]:
    identity_of(request)
    exported = export_skill(_skill(deps_of(request), skill_id))
    return {"yaml": exported.yaml, "content_hash": exported.sha256}
