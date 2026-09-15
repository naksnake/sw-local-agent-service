"""Test-loop templates (CLAUDE.md §10.3 PLAN): `templates/factory/<id>.yaml`, and the
compiler that turns one into a plan for one unit on one station.

The shipped template is a Python mapping rendered to YAML (a test keeps them in step);
templates a line writes land as `Factory/Templates/<id>.template.json` until a YAML reader
is an approved dependency. `$station`, `$unit_sn` and `$mes_ticket_no` in a template are
filled in at compile time; the executor adds the lease at the start and the release at the
end, so a held station is simply one whose run stopped before the release.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import Field, ValidationError

from slas_factory_executor.primitives import PRIMITIVES, risk_of
from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Plan, Step


class TemplateError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class TemplateStep(SlasModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    primitive: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    args: dict[str, Any] = Field(default_factory=dict)


class TestLoopTemplate(SlasModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1)
    #: Skills the loop uses; each must be enabled for the Factory Agent on this installation.
    skills: list[str] = Field(default_factory=list)
    steps: list[TemplateStep] = Field(min_length=1, max_length=50)
    sop_summary: dict[str, str] = Field(default_factory=dict)

    def sentence(self) -> str:
        skills = f", using the {', '.join(self.skills)} skill" if self.skills else ""
        return f"{self.name}: {len(self.steps)} steps{skills}."


FINAL_TEST_9_STEPS: Final[dict[str, Any]] = {
    "id": "final-test-9-steps",
    "name": "Final test, 9 steps",
    "version": "1.0.0",
    "description": (
        "Power the unit on, log in to the station and start BurnIn, wait for the test, read the "
        "result, the sensors and the event log, back up the station, decide with 3 voters."
    ),
    "skills": ["station-login-burnin"],
    "steps": [
        {
            "id": "lease",
            "primitive": "lease_station",
            "title": "Lease the station and bind the unit",
            "args": {
                "station": "$station",
                "unit_sn": "$unit_sn",
                "mes_ticket_no": "$mes_ticket_no",
            },
        },
        {
            "id": "power-on",
            "primitive": "station_command",
            "title": "Power the unit on through the fixture",
            "args": {"station": "$station", "command": ["fixture-ctl", "power", "on"]},
        },
        {
            "id": "login-burnin",
            "primitive": "skill",
            "title": "Log in to the station and start BurnIn",
            "args": {
                "station": "$station",
                "skill_id": "station-login-burnin",
                "inputs": {"user": "operator"},
                "secret_refs": {"password": "env:STATION_OPERATOR_PASSWORD"},
            },
        },
        {
            "id": "wait-complete",
            "primitive": "wait_for_screen",
            "title": "Wait for BurnIn to report the test complete",
            "args": {"station": "$station", "text": "Test complete", "timeout_s": 7200},
        },
        {
            "id": "result",
            "primitive": "read_result",
            "title": "Read the BurnIn result",
            "args": {"station": "$station", "command": ["burnin-ctl", "result", "--json"]},
        },
        {
            "id": "sensors",
            "primitive": "read_sensors",
            "title": "Read the sensors and compare with the limits",
            "args": {
                "station": "$station",
                "command": ["sensors-ctl", "read", "--json"],
                "limits": {"cpu_temp_c": 85, "gpu_temp_c": 85},
            },
        },
        {
            "id": "event-log",
            "primitive": "check_event_log",
            "title": "Check that the event log is empty",
            "args": {"station": "$station", "command": ["evlog", "dump"]},
        },
        {
            "id": "backup",
            "primitive": "backup_station",
            "title": "Back up the station state",
            "args": {"station": "$station"},
        },
        {
            "id": "verdict",
            "primitive": "verdict",
            "title": "Decide PASS or FAIL",
            "args": {"station": "$station"},
        },
    ],
    "sop_summary": {
        "en": "One unit through the final test on the station, decided by three voters.",
        "zh": "一台機器在測試站完成最終測試，由三位投票者裁定。",
    },
}

TEMPLATES: Final[dict[str, dict[str, Any]]] = {"final-test-9-steps": FINAL_TEST_9_STEPS}

TEMPLATE_FILE_HEADER: Final = (
    "Factory test-loop template for SW Local Agent Service (CLAUDE.md §10.3).\n"
    "Rendered from slas_factory_executor.templates; a unit test keeps this file and the code in "
    "step.\n"
    "$station, $unit_sn and $mes_ticket_no are filled in when a job is planned; the executor "
    "adds the\nlease at the start and the release at the end."
)


def template_from_mapping(data: object, *, source: str = "<memory>") -> TestLoopTemplate:
    try:
        template = TestLoopTemplate.model_validate(data)
    except ValidationError as exc:
        raise TemplateError(
            ThreePartMessage(
                f"The template in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; every step needs an id, a primitive, a title and its arguments.",
            )
        ) from exc
    for step in template.steps:
        if step.primitive not in PRIMITIVES:
            raise TemplateError(
                ThreePartMessage(
                    f"The template {template.id} uses the unknown verb {step.primitive!r} "
                    f"in step {step.id}.",
                    "A test loop may only use the Factory primitives in "
                    "plans/primitives/factory.yaml.",
                    "Use one of those verbs, or a skill for GUI steps.",
                )
            )
        spec = PRIMITIVES[step.primitive]
        missing = [a for a in spec.required if a not in step.args]
        if missing:
            raise TemplateError(
                ThreePartMessage(
                    f"Step {step.id} of {template.id} is missing {', '.join(missing)} "
                    f"for {step.primitive}.",
                    f"{step.primitive} needs {', '.join(spec.required)}.",
                    "Add the arguments to the template step.",
                )
            )
    return template


def default_templates() -> dict[str, TestLoopTemplate]:
    return {
        name: template_from_mapping(data, source=f"templates/factory/{name}.yaml")
        for name, data in TEMPLATES.items()
    }


def load_templates(templates_dir: Path) -> dict[str, TestLoopTemplate]:
    """The shipped templates plus every `<id>.template.json` a line has added."""
    templates = default_templates()
    if templates_dir.is_dir():
        for path in sorted(templates_dir.glob("*.template.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            template = template_from_mapping(data, source=str(path))
            templates[template.id] = template
    return templates


def _fill(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return variables.get(value, value) if value.startswith("$") else value
    if isinstance(value, list):
        return [_fill(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, variables) for k, v in value.items()}
    return value


def compile_template(
    template: TestLoopTemplate,
    *,
    job_id: str,
    station: str,
    unit_sn: str,
    mes_ticket_no: str,
    now: datetime,
) -> Plan:
    variables = {"$station": station, "$unit_sn": unit_sn, "$mes_ticket_no": mes_ticket_no}
    total = len(template.steps) + 1  # + release
    steps: list[Step] = []
    for index, tstep in enumerate(template.steps, start=1):
        args = dict(_fill(tstep.args, variables))
        args.setdefault("station", station)
        args["loop_step"] = index
        args["loop_steps"] = total
        steps.append(
            Step(
                id=tstep.id,
                n=index,
                primitive=tstep.primitive,
                title=tstep.title,
                args=args,
                risk=risk_of(tstep.primitive),
            )
        )
    steps.append(
        Step(
            id="release",
            n=total,
            primitive="release_station",
            title=f"Release {station}",
            args={"station": station, "loop_step": total, "loop_steps": total},
            risk="safe",
        )
    )
    destructive = [s for s in steps if s.risk == "destructive"]
    summary = (
        f"{template.name} for unit {unit_sn} on {station}: {total} steps"
        + (f", using the {', '.join(template.skills)} skill" if template.skills else "")
        + (
            f"; {len(destructive)} destructive "
            f"{'step needs' if len(destructive) == 1 else 'steps need'} your approval."
            if destructive
            else ". PASS needs 3 of 3 voters; anything else holds the station for the line lead."
        )
    )
    return Plan(id=f"plan-{job_id}", job_id=job_id, summary=summary, steps=steps, created_at=now)


def render_template_yaml(data: Mapping[str, Any], *, header: str = TEMPLATE_FILE_HEADER) -> str:
    template = template_from_mapping(data)
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    lines += [
        f"id: {template.id}",
        f"name: {json.dumps(template.name, ensure_ascii=False)}",
        f"version: {template.version}",
        f"description: {json.dumps(template.description, ensure_ascii=False)}",
        f"skills: [{', '.join(template.skills)}]",
        "steps:",
    ]
    for step in template.steps:
        lines.append(f"  - id: {step.id}")
        lines.append(f"    primitive: {step.primitive}")
        lines.append(f"    title: {json.dumps(step.title, ensure_ascii=False)}")
        lines.append("    args:")
        for key, value in step.args.items():
            lines.append(f"      {key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("sop_summary:")
    for lang, text in template.sop_summary.items():
        lines.append(f"  {lang}: {json.dumps(text, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"
