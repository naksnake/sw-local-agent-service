"""The Validation plan verbs (CLAUDE.md §10.2) and `plans/schema/plan.schema.json`.

A plan step is `{id, n, primitive, title, args, risk, when}` from `slas_schemas.plan`; this
table says which primitives a Validation plan may use, what each needs, and which are
destructive and therefore need a per-run human approval (INV-7). The schema file and
`plans/primitives/validation.yaml` are rendered from this table; a test keeps them in step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from slas_schemas.plan import MAX_STEPS

SCHEMA_ID: Final = "https://slas.local/schemas/plan.schema.json"
POWER_KINDS: Final[tuple[str, ...]] = ("warm", "dc", "ac")
STRESS_TOOLS: Final[tuple[str, ...]] = ("stress-ng", "fio", "perftest", "memtester")
DIAG_TOOLS: Final[tuple[str, ...]] = ("nvqual", "mft", "smartctl", "ipmitool", "dcgmi")


@dataclass(frozen=True)
class PlanPrimitive:
    name: str
    description: str
    risk: str  # safe · caution · destructive
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    #: For `power_cycle`: which `kind` values are destructive (need approval).
    destructive_when: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def known_args(self) -> tuple[str, ...]:
        return (*self.required, *self.optional)


PRIMITIVES: Final[dict[str, PlanPrimitive]] = {
    p.name: p
    for p in (
        PlanPrimitive(
            "lease_target",
            "Take the exclusive lease on the target for this run.",
            "safe",
            ("target",),
        ),
        PlanPrimitive(
            "console_on",
            "Start capturing the serial console (SOL) and syslog.",
            "safe",
            ("target",),
        ),
        PlanPrimitive(
            "baseline_snapshot",
            "Record inventory, PCIe link state, firmware, counters and SEL as the baseline.",
            "safe",
            ("target",),
        ),
        PlanPrimitive(
            "power_cycle",
            "One power cycle: ARM → QUIESCE → ACT → SETTLE → VERIFY against the baseline. "
            "Kind warm (OS reboot), dc (BMC stays up) or ac (BMC cycles too; destructive).",
            "caution",
            ("target", "kind", "cycle"),
            ("settle_s", "total_cycles"),
            {"kind": ("ac",)},
        ),
        PlanPrimitive("sel_snapshot", "Read the BMC event log.", "safe", ("target",)),
        PlanPrimitive(
            "inventory_snapshot",
            "Read inventory, PCIe link state and firmware versions.",
            "safe",
            ("target",),
        ),
        PlanPrimitive(
            "stress",
            "Run a stress tool on the target over SSH for a bounded time.",
            "caution",
            ("target", "tool"),
            ("args", "duration_s"),
        ),
        PlanPrimitive(
            "run_diag",
            "Run a vendor diagnostic on the target over SSH.",
            "caution",
            ("target", "tool"),
            ("args", "timeout_s"),
        ),
        PlanPrimitive(
            "firmware_flash",
            "Flash a firmware image onto a component. Destructive.",
            "destructive",
            ("target", "component", "image_ref"),
        ),
        PlanPrimitive(
            "secure_erase",
            "Securely erase a storage device. Destructive.",
            "destructive",
            ("target", "device"),
        ),
        PlanPrimitive(
            "bios_reset",
            "Reset BIOS settings to defaults. Destructive.",
            "destructive",
            ("target",),
        ),
        PlanPrimitive(
            "raid_reconfigure",
            "Change the RAID layout. Destructive.",
            "destructive",
            ("target", "layout"),
        ),
        PlanPrimitive(
            "collect_logs",
            "Collect console, syslog, SEL and tool output onto the ticket.",
            "safe",
            ("target",),
        ),
        PlanPrimitive("release_target", "Release the lease.", "safe", ("target",)),
    )
}

#: Destructive by name or by argument (`power_cycle kind=ac`); these need approval (INV-7).
APPROVAL_KINDS: Final[tuple[str, ...]] = (
    "ac_cycle",
    "firmware_flash",
    "secure_erase",
    "bios_reset",
    "raid_reconfigure",
)


def risk_of(primitive: str, args: dict[str, Any]) -> str:
    spec = PRIMITIVES[primitive]
    for arg, values in spec.destructive_when.items():
        if str(args.get(arg, "")) in values:
            return "destructive"
    return spec.risk


def approval_kind(primitive: str, args: dict[str, Any]) -> str | None:
    """The guardrail name of a destructive step, or None."""
    if primitive == "power_cycle" and str(args.get("kind")) == "ac":
        return "ac_cycle"
    return primitive if PRIMITIVES[primitive].risk == "destructive" else None


_ARG_TYPES: Final[dict[str, dict[str, Any]]] = {
    "target": {
        "type": "string",
        "minLength": 1,
        "description": "An opaque target reference, never a credential.",
    },
    "kind": {"enum": list(POWER_KINDS)},
    "cycle": {"type": "integer", "minimum": 1},
    "settle_s": {"type": "integer", "minimum": 1},
    "total_cycles": {
        "type": "integer",
        "minimum": 1,
        "description": "How many power cycles the whole plan has, so the cycle map can be drawn.",
    },
    "tool": {"type": "string", "enum": sorted({*STRESS_TOOLS, *DIAG_TOOLS})},
    "args": {"type": "array", "items": {"type": "string"}},
    "duration_s": {"type": "integer", "minimum": 1, "maximum": 86400},
    "timeout_s": {"type": "integer", "minimum": 1, "maximum": 86400},
    "component": {"type": "string", "minLength": 1},
    "image_ref": {
        "type": "string",
        "minLength": 1,
        "description": "A reference into the firmware store, never a path a model chose.",
    },
    "device": {"type": "string", "minLength": 1},
    "layout": {"type": "string", "minLength": 1},
}


def build_plan_schema() -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for name, spec in PRIMITIVES.items():
        variants.append(
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
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "SW Local Agent Service — Validation plan",
        "description": (
            "A compiled Validation plan: deterministic steps a state machine performs "
            "(CLAUDE.md §10.2). Destructive steps need a per-run human approval (INV-7)."
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
    "Validation plan primitives for SW Local Agent Service (CLAUDE.md §10.2).\n"
    "Rendered from slas_hal.primitives.PRIMITIVES; a unit test keeps file and code in step.\n"
    "Destructive steps (and power_cycle kind: ac) need a per-run human approval (INV-7)."
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
        for arg, values in spec.destructive_when.items():
            lines.append(f"    destructive_when: {{ {arg}: [{', '.join(values)}] }}")
    lines.append(f"approval_kinds: [{', '.join(APPROVAL_KINDS)}]")
    return "\n".join(lines) + "\n"
