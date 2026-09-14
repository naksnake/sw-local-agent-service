"""The Factory plan verbs (CLAUDE.md §10.3) and `plans/primitives/factory.yaml`.

Every verb acts on a station through the station runner; none touches the platform host
(INV-4). Reading the screen, the sensors and the event log are observations; `verdict` is
the deterministic gate plus the Consensus Router (PASS needs 3 of 3, INV-11); the only
destructive verb is a station configuration change (INV-7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from slas_schemas.plan import MAX_STEPS

SCHEMA_ID: Final = "https://slas.local/schemas/factory-plan.schema.json"


@dataclass(frozen=True)
class FactoryPrimitive:
    name: str
    description: str
    risk: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def known_args(self) -> tuple[str, ...]:
        return (*self.required, *self.optional, *COMMON_OPTIONAL)


#: Present on every step so the page can draw the whole test-step map from step one.
COMMON_OPTIONAL: Final[tuple[str, ...]] = ("loop_step", "loop_steps")

PRIMITIVES: Final[dict[str, FactoryPrimitive]] = {
    p.name: p
    for p in (
        FactoryPrimitive(
            "lease_station",
            "Take the exclusive lease on the station and bind the unit's serial number.",
            "safe",
            ("station", "unit_sn"),
            ("mes_ticket_no",),
        ),
        FactoryPrimitive(
            "station_command",
            "Run one allowlisted program on the station (fixture power, tool control).",
            "caution",
            ("station", "command"),
            ("expect_exit", "timeout_s"),
        ),
        FactoryPrimitive(
            "skill",
            "Run an imported skill's GUI steps on the station through the runner; every step is "
            "screenshot before and after.",
            "caution",
            ("station", "skill_id"),
            ("inputs", "secret_refs"),
        ),
        FactoryPrimitive(
            "wait_for_screen",
            "Wait until a text appears on the station's screen.",
            "safe",
            ("station", "text"),
            ("timeout_s",),
        ),
        FactoryPrimitive(
            "read_result",
            "Read the vendor test result as JSON from the station.",
            "safe",
            ("station", "command"),
        ),
        FactoryPrimitive(
            "read_sensors",
            "Read the unit's sensors as JSON and compare them with the limits.",
            "safe",
            ("station", "command", "limits"),
        ),
        FactoryPrimitive(
            "check_event_log",
            "Dump the unit's event log; it must be empty.",
            "safe",
            ("station", "command"),
        ),
        FactoryPrimitive(
            "verdict",
            "Decide: the deterministic gate (result, sensors, event log), then 3 of 3 voters "
            "for PASS; anything else holds the station for the line lead.",
            "safe",
            ("station",),
        ),
        FactoryPrimitive(
            "backup_station",
            "Collect the station's config, recent logs and application versions into "
            "Backups/stations/<station>/<ticket>.",
            "safe",
            ("station",),
        ),
        FactoryPrimitive(
            "release_station",
            "Release the station lease.",
            "safe",
            ("station",),
        ),
        FactoryPrimitive(
            "station_config_change",
            "Change a station setting. Destructive: needs a per-run approval.",
            "destructive",
            ("station", "setting", "value"),
        ),
    )
}

_ARG_TYPES: Final[dict[str, dict[str, Any]]] = {
    "station": {"type": "string", "minLength": 1},
    "unit_sn": {"type": "string", "minLength": 1},
    "mes_ticket_no": {"type": "string"},
    "command": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    "expect_exit": {"type": "integer"},
    "timeout_s": {"type": "integer", "minimum": 1, "maximum": 86400},
    "skill_id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$"},
    "inputs": {"type": "object"},
    "secret_refs": {
        "type": "object",
        "additionalProperties": {"type": "string", "pattern": "^(env|vault|file):"},
        "description": "Skill input → credential reference; the value is resolved at dispatch.",
    },
    "text": {"type": "string", "minLength": 1},
    "limits": {"type": "object", "additionalProperties": {"type": "number"}},
    "setting": {"type": "string", "minLength": 1},
    "value": {"type": "string"},
    "loop_step": {"type": "integer", "minimum": 1},
    "loop_steps": {"type": "integer", "minimum": 1},
}


def risk_of(primitive: str) -> str:
    return PRIMITIVES[primitive].risk


def build_plan_schema() -> dict[str, Any]:
    variants = [
        {
            "type": "object",
            "description": spec.description,
            "properties": {
                "primitive": {"const": name},
                "args": {
                    "type": "object",
                    "properties": {arg: dict(_ARG_TYPES[arg]) for arg in spec.known_args()},
                    "required": list(spec.required),
                    "additionalProperties": False,
                },
            },
            "required": ["primitive", "args"],
        }
        for name, spec in PRIMITIVES.items()
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "SW Local Agent Service — Factory plan",
        "description": (
            "A compiled Factory test loop: deterministic steps the factory executor performs on "
            "a station through the station runner (CLAUDE.md §10.3). PASS needs 3 of 3 voters."
        ),
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "job_id": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "created_at": {"type": "string", "format": "date-time"},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STEPS,
                "items": {"$ref": "#/$defs/step"},
            },
        },
        "required": ["id", "job_id", "summary", "steps", "created_at"],
        "additionalProperties": False,
        "$defs": {
            "step": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$"},
                    "n": {"type": "integer", "minimum": 1},
                    "primitive": {"enum": list(PRIMITIVES)},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "args": {"type": "object"},
                    "risk": {"enum": ["safe", "caution", "destructive"]},
                    "when": {"type": ["string", "null"]},
                },
                "required": ["id", "n", "primitive", "title", "args", "risk"],
                "additionalProperties": False,
                "oneOf": variants,
            }
        },
    }


def render_plan_schema() -> str:
    return json.dumps(build_plan_schema(), indent=2, ensure_ascii=False) + "\n"


PRIMITIVES_FILE_HEADER: Final = (
    "Factory plan primitives for SW Local Agent Service (CLAUDE.md §10.3).\n"
    "Rendered from slas_factory_executor.primitives.PRIMITIVES; a unit test keeps file and "
    "code in step.\n"
    "Every verb acts on a station through the station runner; PASS needs 3 of 3 voters (INV-11);\n"
    "station_config_change is destructive and needs a per-run approval (INV-7)."
)


def render_primitives_yaml(*, header: str = PRIMITIVES_FILE_HEADER) -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    lines.append("version: 1")
    lines.append("primitives:")
    for spec in PRIMITIVES.values():
        lines.append(f"  {spec.name}:")
        lines.append(f"    description: {json.dumps(spec.description, ensure_ascii=False)}")
        lines.append(f"    risk: {spec.risk}")
        lines.append(f"    required: [{', '.join(spec.required)}]")
        if spec.optional:
            lines.append(f"    optional: [{', '.join(spec.optional)}]")
    lines.append(f"common_optional: [{', '.join(COMMON_OPTIONAL)}]")
    return "\n".join(lines) + "\n"
