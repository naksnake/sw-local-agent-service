"""`/v1/skills`: the library on disk, check, import, per-agent toggles, export, delete
(contract §5, §5.6).

Skills are data (INV-12): a file is parsed with `yaml.safe_load`, validated by
`slas_skills.importer`, stored under `Skills/library/<id>.skill.yaml`; its enablement lives
beside it in `<id>.state.json` (ADR-0013). The acting person comes from the identity
headers; the import checks that person's capabilities and never grants any. A skill is
deleted by the person who imported it or by an administrator (`admin:settings`); deleting
removes the file and its record, never a ticket that already ran it.
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
from slas_skills.importer import ImportReport, import_skill, store_skill, walk
from slas_skills.library import LIBRARY
from slas_skills.schema import Skill, SkillAgent, SkillImportError, SkillStep, parse_skill
from slas_skills.state import AGENT_LABEL, SkillStateError, SkillStateStore, skill_risk

router = APIRouter(prefix="/v1/skills")

SKILL_SUFFIX: Final = ".skill.yaml"
MAX_SKILL_BYTES: Final = 256 * 1024
#: Who may delete any skill, not only the ones they imported.
ADMIN_CAPABILITY: Final = "admin:settings"


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


def library_files(library_dir: Path, skill_id: str) -> list[Path]:
    """The files that hold a skill: `<id>.skill.yaml`, and any other name a person copied it
    in under."""
    found: list[Path] = []
    for path in sorted(library_dir.glob(f"*{SKILL_SUFFIX}")):
        if path.name == f"{skill_id}{SKILL_SUFFIX}":
            found.append(path)
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        inner = data.get("skill") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("id") == skill_id:
            found.append(path)
    return found


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


def step_line(step: SkillStep) -> str:
    """One step as a person reads it: `run: ruff check .`, `redfish: get_sel on {{ target }}`."""
    args = step.args
    if isinstance(args.get("command"), list):
        text = " ".join(str(a) for a in args["command"])
        if "target" in args:
            text += f" on {args['target']}"
    elif "action" in args:
        text = f"{args['action']} on {args.get('target', 'the target')}"
    else:
        text = ", ".join(f"{k} {v}" for k, v in args.items() if k not in ("then", "else"))
    prefix = f"{step.primitive} ({step.id})" if step.id else step.primitive
    line = f"{prefix}: {text}" if text else prefix
    return f"if {step.when}: {line}" if step.when else line


def skill_detail(skill: Skill, state_store: SkillStateStore) -> dict[str, Any]:
    """The row plus what the Skills page shows when a skill is opened."""
    state = state_store.load(skill.id)
    return {
        **skill_row(skill, state_store),
        "description": skill.description,
        "steps": [step_line(step) for _, step in walk(skill.steps)],
        "inputs": [
            {"name": name, "type": spec.type, "required": spec.required}
            for name, spec in skill.inputs.items()
        ],
        "imported_by": state.imported_by if state is not None else None,
        "imported_at": state.imported_at.isoformat() if state is not None else None,
        "yaml": export_skill(skill).yaml,
    }


def report_json(report: ImportReport) -> dict[str, Any]:
    return report.model_dump(mode="json", by_alias=True)


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


def _parse_yaml(text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ServiceError(
            400,
            ThreePartMessage(
                "The skill file is not valid YAML.",
                str(exc).splitlines()[0] if str(exc) else "The parser found a syntax error.",
                "Fix the file and import it again; the format is CLAUDE.md §6.1.",
            ),
        ) from exc


def _checked(identity: Identity, text: str) -> ImportReport:
    try:
        return import_skill(
            _parse_yaml(text), principal=principal_of(identity), source="the skill file"
        )
    except SkillImportError as exc:
        raise ServiceError(400, exc.message) from exc


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


@router.get("/examples")
def examples_route(request: Request) -> list[dict[str, Any]]:
    """The skills that ship with the platform, to start from or import as they are."""
    identity_of(request)
    deps = deps_of(request)
    library = _library(deps)
    answer: list[dict[str, Any]] = []
    for skill_id, mapping in LIBRARY.items():
        skill = parse_skill(mapping, source=f"{skill_id}{SKILL_SUFFIX}")
        answer.append(
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "agents": list(skill.agents),
                "requires": [c.value for c in skill.requires],
                "risk": skill_risk(skill),
                "installed": skill.id in library,
                "yaml": export_skill(skill).yaml,
            }
        )
    return answer


@router.post("/validate")
def validate_route(request: Request, body: ImportBody) -> dict[str, Any]:
    """Every check an import makes, without storing anything: the editor's Check button."""
    identity = identity_of(request)
    deps = deps_of(request)
    report = _checked(identity, body.yaml)
    existing = _library(deps).get(report.skill.id)
    answer = report_json(report)
    answer["replaces"] = existing.version if existing is not None else None
    answer["steps"] = [step_line(step) for _, step in walk(report.skill.steps)]
    return answer


@router.post("/import")
def import_route(request: Request, body: ImportBody) -> dict[str, Any]:
    identity = identity_of(request)
    deps = deps_of(request)
    report = _checked(identity, body.yaml)
    store_skill(report, deps.settings.skills_library)
    state = deps.skill_state.record_import(report.skill, by=identity.user, now=deps.clock.now())
    deps.log.info("skill.imported", skill_id=report.skill.id, version=report.skill.version)
    answer = report_json(report)
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


@router.get("/{id}")
def detail_route(request: Request, skill_id: str = PathParam(alias="id")) -> dict[str, Any]:
    identity_of(request)
    deps = deps_of(request)
    return skill_detail(_skill(deps, skill_id), deps.skill_state)


@router.delete("/{id}")
def delete_route(request: Request, skill_id: str = PathParam(alias="id")) -> dict[str, Any]:
    identity = identity_of(request)
    deps = deps_of(request)
    skill = _skill(deps, skill_id)
    state = deps.skill_state.load(skill.id)
    owner = state.imported_by if state is not None else None
    if owner != identity.user and not identity.has(ADMIN_CAPABILITY):
        raise ServiceError(
            403,
            ThreePartMessage(
                f"You can't delete {skill.name}.",
                f"It was imported by {owner}; only they or an administrator may delete it."
                if owner
                else "It has no import record here, so only an administrator may delete it.",
                "Ask them, or switch it off for your agent instead.",
            ),
        )
    for path in library_files(deps.settings.skills_library, skill.id):
        path.unlink(missing_ok=True)
    deps.skill_state.path(skill.id).unlink(missing_ok=True)
    deps.log.info("skill.deleted", skill_id=skill.id, version=skill.version, by=identity.user)
    agents = ", ".join(AGENT_LABEL.get(a, a) for a in skill.agents)
    return {
        "sentence": (
            f"{skill.name} v{skill.version} was deleted; {agents} can no longer use it. "
            "Tickets that already ran it keep their record."
        )
    }
