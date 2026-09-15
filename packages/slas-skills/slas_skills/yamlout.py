"""Render a skill mapping as the YAML people read and share (§6.3 style).

Steps are written one per line as `- verb: { arg: value, … }`, exactly like the examples
in CLAUDE.md. Strings that need it are double-quoted with JSON escaping, which YAML reads.
Parsing YAML back is one function that arrives with the approved YAML dependency; until
then the shipped library files are rendered from Python mappings and kept in step by a test.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from slas_skills.schema import Skill, SkillStep

_PLAIN = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
_RESERVED = {"true", "false", "yes", "no", "null", "on", "off", "~"}


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if _PLAIN.match(text) and text.lower() not in _RESERVED and not text.isdigit():
        return text
    return json.dumps(text, ensure_ascii=False)


def _flow(mapping: Mapping[str, Any]) -> str:
    parts = []
    for key, value in mapping.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(_scalar(item) for item in value) + "]"
        elif isinstance(value, Mapping):
            rendered = _flow(value)
        else:
            rendered = _scalar(value)
        parts.append(f"{key}: {rendered}")
    return "{ " + ", ".join(parts) + " }"


def _steps(steps: list[Mapping[str, Any]], indent: int) -> list[str]:
    pad = " " * indent
    lines: list[str] = []
    for step in steps:
        verb = next(key for key in step if key not in ("id", "when"))
        args = dict(step[verb])
        branches = {k: args.pop(k) for k in ("then", "else") if k in args}
        lines.append(f"{pad}- {verb}: {_flow(args)}")
        for meta in ("id", "when"):
            if meta in step:
                lines.append(f"{pad}  {meta}: {_scalar(step[meta])}")
        for name, branch in branches.items():
            lines.append(f"{pad}  {name}:")
            lines.extend(_steps(branch, indent + 4))
    return lines


def _block(value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                if all(not isinstance(v, Mapping | list) for v in item.values()):
                    lines.append(f"{pad}{key}: {_flow(item)}")
                else:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_block(item, indent + 2))
            elif isinstance(item, list):
                if all(not isinstance(v, Mapping | list) for v in item):
                    lines.append(f"{pad}{key}: [" + ", ".join(_scalar(v) for v in item) + "]")
                else:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_block(item, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return lines
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                first = True
                for key, sub in item.items():
                    prefix = f"{pad}- " if first else f"{pad}  "
                    first = False
                    if isinstance(sub, Mapping | list):
                        out.append(f"{prefix}{key}:")
                        out.extend(_block(sub, indent + 4))
                    else:
                        out.append(f"{prefix}{key}: {_scalar(sub)}")
            else:
                out.append(f"{pad}- {_scalar(item)}")
        return out
    return [f"{pad}{_scalar(value)}"]


def render_skill_yaml(data: Mapping[str, Any], *, header: str = "") -> str:
    skill = data["skill"]
    if not isinstance(skill, Mapping):
        raise TypeError("a skill mapping has a top-level 'skill' mapping")
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append("skill:")
    for key, value in skill.items():
        if key == "steps":
            lines.append("  steps:")
            lines.extend(_steps(list(value), 4))
        elif key == "inputs" and isinstance(value, Mapping):
            lines.append("  inputs:")
            for name, spec in value.items():
                lines.append(f"    {name}: {_flow(spec)}")
        elif isinstance(value, Mapping):
            if all(not isinstance(v, Mapping | list) for v in value.values()):
                lines.append(f"  {key}: {_flow(value)}")
            else:
                lines.append(f"  {key}:")
                lines.extend(_block(value, 4))
        elif isinstance(value, list):
            lines.append(f"  {key}: [" + ", ".join(_scalar(v) for v in value) + "]")
        else:
            lines.append(f"  {key}: {_scalar(value)}")
    return "\n".join(lines) + "\n"


def step_to_mapping(step: SkillStep) -> dict[str, Any]:
    args: dict[str, Any] = dict(step.args)
    if step.then:
        args["then"] = [step_to_mapping(s) for s in step.then]
    if step.otherwise:
        args["else"] = [step_to_mapping(s) for s in step.otherwise]
    out: dict[str, Any] = {step.primitive: args}
    if step.id:
        out["id"] = step.id
    if step.when:
        out["when"] = step.when
    return out


def skill_to_mapping(skill: Skill, *, strip_secret_defaults: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": skill.id,
        "name": skill.name,
        "version": skill.version,
    }
    if skill.description:
        body["description"] = skill.description
    body["agents"] = list(skill.agents)
    body["requires"] = [c.value for c in skill.requires]
    body["timeout_s"] = skill.timeout_s
    if skill.inputs:
        inputs: dict[str, Any] = {}
        for name, spec in skill.inputs.items():
            entry: dict[str, Any] = {"type": spec.type}
            if spec.required:
                entry["required"] = True
            if spec.default is not None and not (strip_secret_defaults and spec.type == "secret"):
                entry["default"] = spec.default
            if spec.description:
                entry["description"] = spec.description
            inputs[name] = entry
        body["inputs"] = inputs
    body["steps"] = [step_to_mapping(step) for step in skill.steps]
    if skill.outputs:
        body["outputs"] = {name: {"from": spec.source} for name, spec in skill.outputs.items()}
    body["on_failure"] = (
        skill.on_failure if isinstance(skill.on_failure, str) else skill.on_failure.model_dump()
    )
    if skill.sop_summary:
        body["sop_summary"] = skill.sop_summary.model_dump()
    return {"skill": body}
