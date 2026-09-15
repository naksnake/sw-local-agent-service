"""COMPILE (CLAUDE.md §10.2): suite items → plan.yaml, with the reject rules.

    reject unknown primitive · count > cap · destructive without approval flag

Known actions map to primitives deterministically; anything else may go to the `Compiler`
protocol (the LLM as compiler, once, behind the gateway) and is still checked against the
same rules. Cycles are unrolled into one `power_cycle` step each so the kernel's journal
gives crash recovery per cycle.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import Field

from slas_hal.primitives import DIAG_TOOLS, PRIMITIVES, STRESS_TOOLS, approval_kind, risk_of
from slas_orchestrator.validation.suite import Suite, SuiteItem
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import MAX_STEPS, Plan, Step
from slas_validation_executor.guardrails import Guardrails, check_plan


class CompiledItem(SlasModel):
    primitive: str
    args: dict[str, object] = Field(default_factory=dict)


class Compiler(Protocol):
    """The model as compiler for actions the table does not know; its answer is checked."""

    def compile_item(self, item: SuiteItem) -> CompiledItem | None: ...


class FakeCompiler:
    def __init__(self, answers: dict[str, CompiledItem] | None = None) -> None:
        self.answers = dict(answers or {})
        self.asked: list[str] = []

    def compile_item(self, item: SuiteItem) -> CompiledItem | None:
        self.asked.append(item.action)
        return self.answers.get(item.action.lower())


class PlanCompileError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


_RULES: tuple[tuple[re.Pattern[str], str, dict[str, object]], ...] = (
    (
        re.compile(
            r"\bac\b.*\b(?:power\s*)?cycl|\bac cycle|power (?:cord|cable) pull|\bpdu\b", re.I
        ),
        "power_cycle",
        {"kind": "ac"},
    ),
    (
        re.compile(
            r"\bdc\b.*\bcycl|\bdc cycle|power cycle|cold (?:boot|reboot)|power off.*power on", re.I
        ),
        "power_cycle",
        {"kind": "dc"},
    ),
    (
        re.compile(r"warm (?:boot|reboot)|\breboot\b|graceful restart|os restart", re.I),
        "power_cycle",
        {"kind": "warm"},
    ),
    (re.compile(r"baseline", re.I), "baseline_snapshot", {}),
    (re.compile(r"\bsel\b|event log", re.I), "sel_snapshot", {}),
    (
        re.compile(r"inventory|lnksta|link (?:state|width|speed)|firmware version", re.I),
        "inventory_snapshot",
        {},
    ),
    (
        re.compile(r"firmware (?:flash|update|upgrade)|flash (?:the )?(?:bios|bmc|firmware)", re.I),
        "firmware_flash",
        {},
    ),
    (re.compile(r"secure erase|sanitize|crypto erase", re.I), "secure_erase", {}),
    (re.compile(r"bios (?:reset|defaults?)|load (?:setup )?defaults", re.I), "bios_reset", {}),
    (re.compile(r"\braid\b", re.I), "raid_reconfigure", {}),
    (re.compile(r"collect (?:the )?logs?|gather logs", re.I), "collect_logs", {}),
)
_TOOL_WORDS = {tool: tool for tool in (*STRESS_TOOLS, *DIAG_TOOLS)}
_TOOL_WORDS.update(
    {
        "stress": "stress-ng",
        "burn": "stress-ng",
        "nvidia qual": "nvqual",
        "mellanox": "mft",
        "smart": "smartctl",
    }
)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def map_action(item: SuiteItem) -> CompiledItem | None:
    """Deterministic table first; None means the compiler (a model) may be asked."""
    text = f"{item.title} {item.action} {' '.join(f'{k} {v}' for k, v in item.params.items())}"
    for pattern, primitive, args in _RULES:
        if pattern.search(text):
            merged: dict[str, object] = {**args}
            if primitive == "power_cycle" and "settle_s" in item.params:
                merged["settle_s"] = int(item.params["settle_s"])
            if primitive == "firmware_flash":
                merged["component"] = item.params.get("component", "unspecified")
                merged["image_ref"] = item.params.get(
                    "image", item.params.get("image_ref", "unspecified")
                )
            if primitive == "secure_erase":
                merged["device"] = item.params.get("device", "unspecified")
            if primitive == "raid_reconfigure":
                merged["layout"] = item.params.get("layout", "unspecified")
            return CompiledItem(primitive=primitive, args=merged)
    lowered = text.lower()
    for word, tool in _TOOL_WORDS.items():
        if word in lowered:
            primitive = "stress" if tool in STRESS_TOOLS else "run_diag"
            tool_args: dict[str, object] = {"tool": tool}
            if item.params.get("args"):
                tool_args["args"] = item.params["args"].split()
            if item.params.get("duration_s"):
                tool_args["duration_s" if primitive == "stress" else "timeout_s"] = int(
                    item.params["duration_s"]
                )
            return CompiledItem(primitive=primitive, args=tool_args)
    return None


def compile_suite(
    suite: Suite,
    *,
    job_id: str,
    target: str,
    guardrails: Guardrails,
    now: datetime,
    compiler: Compiler | None = None,
) -> Plan:
    steps: list[Step] = []

    def add(step_id: str, primitive: str, title: str, args: dict[str, object]) -> None:
        full = {"target": target, **args}
        steps.append(
            Step(
                id=step_id,
                n=len(steps) + 1,
                primitive=primitive,
                title=title[:200],
                args=full,
                risk=risk_of(primitive, full),
            )
        )

    add("lease", "lease_target", f"Lease {target} for this run", {})
    add("console", "console_on", "Capture the serial console and syslog", {})
    add("baseline", "baseline_snapshot", "Record the baseline", {})

    # First pass: every item must compile and pass the reject rules before any step exists,
    # so the cycle titles can say "cycle 3 of 5" and a bad last row rejects the whole suite.
    compiled_items: list[tuple[SuiteItem, CompiledItem]] = []
    grand_total = 0
    for item in suite.items:
        compiled = map_action(item)
        if compiled is None and compiler is not None:
            compiled = compiler.compile_item(item)
        if compiled is None or compiled.primitive not in PRIMITIVES:
            named = compiled.primitive if compiled else item.action
            raise PlanCompileError(
                ThreePartMessage(
                    f"Step {item.n} ({item.title}) names no known action: {named!r}.",
                    "A plan may only use the Validation primitives in "
                    "plans/primitives/validation.yaml.",
                    "Reword the step (for example `DC cycle x25, settle 60 s`), or remove it.",
                )
            )
        spec = PRIMITIVES[compiled.primitive]
        missing = [
            a for a in spec.required if a != "target" and a not in compiled.args and a != "cycle"
        ]
        if missing:
            raise PlanCompileError(
                ThreePartMessage(
                    f"Step {item.n} ({item.title}) is missing {', '.join(missing)} "
                    f"for {compiled.primitive}.",
                    f"{compiled.primitive} needs "
                    f"{', '.join(a for a in spec.required if a != 'target')}.",
                    "Add the parameter to the suite row, for example `device=nvme0n1`.",
                )
            )
        kind = approval_kind(compiled.primitive, dict(compiled.args))
        if kind is not None and not item.approved:
            raise PlanCompileError(
                ThreePartMessage(
                    f"Step {item.n} ({item.title}) is destructive ({kind}) and the suite "
                    "does not flag it as approved.",
                    "Destructive steps need a per-run human approval (INV-7); the suite must "
                    "say the author expects one.",
                    "Add `approved` to the step (Approved column: yes), then approve it again "
                    "when the run asks.",
                )
            )
        if compiled.primitive == "power_cycle":
            grand_total += item.cycles
            if grand_total > guardrails.max_cycles_per_run:
                raise PlanCompileError(
                    ThreePartMessage(
                        f"The suite asks for {grand_total} power cycles; the limit is "
                        f"{guardrails.max_cycles_per_run} per run.",
                        "Guardrail max_cycles_per_run (config/guardrails.yaml).",
                        "Split the suite into several runs, or lower the cycle count.",
                    )
                )
        compiled_items.append((item, compiled))

    # Second pass: unroll.
    total_cycles = 0
    for item, compiled in compiled_items:
        if compiled.primitive == "power_cycle":
            power_kind = str(compiled.args.get("kind", "dc"))
            settle = _as_int(compiled.args.get("settle_s"), guardrails.settle_floor(power_kind))
            for _ in range(item.cycles):
                total_cycles += 1
                add(
                    f"cycle-{total_cycles:03d}",
                    "power_cycle",
                    f"{power_kind.upper()} cycle {total_cycles} of {grand_total}",
                    {
                        "kind": power_kind,
                        "cycle": total_cycles,
                        "settle_s": max(settle, guardrails.settle_floor(power_kind)),
                        "total_cycles": grand_total,
                    },
                )
        else:
            add(f"item-{item.n}", compiled.primitive, item.title, dict(compiled.args))
        if len(steps) > MAX_STEPS - 2:
            raise PlanCompileError(
                ThreePartMessage(
                    f"The plan would have more than {MAX_STEPS} steps.",
                    "Plans are capped so a person can review them (CLAUDE.md §6.1).",
                    "Split the suite into several runs.",
                )
            )
    add("collect", "collect_logs", "Collect console, syslog and SEL onto the ticket", {})
    add("release", "release_target", f"Release {target}", {})

    destructive = [s for s in steps if s.risk == "destructive"]
    needs = "step needs" if len(destructive) == 1 else "steps need"
    summary = (
        f"{suite.title} on {target}: {total_cycles} power cycles and {len(suite.items)} "
        f"suite items, {len(steps)} steps."
        + (
            f" {len(destructive)} destructive {needs} your approval before the run starts."
            if destructive
            else " Nothing destructive."
        )
    )
    plan = Plan(id=f"plan-{job_id}", job_id=job_id, summary=summary, steps=steps, created_at=now)
    problems = check_plan(plan, guardrails)
    if problems:
        raise PlanCompileError(
            ThreePartMessage(
                "The plan breaks a guardrail.",
                " ".join(problems),
                "Change the suite so the plan stays within config/guardrails.yaml.",
            )
        )
    return plan


def render_plan_yaml(plan: Plan) -> str:
    """plan.yaml as people read it: one step per block, values quoted where YAML needs it."""
    lines = [
        "# Validation plan rendered by SW Local Agent Service;",
        "# validate with plans/schema/plan.schema.json.",
        f"id: {plan.id}",
        f"job_id: {plan.job_id}",
        f"summary: {json.dumps(plan.summary, ensure_ascii=False)}",
        f"created_at: {plan.created_at.isoformat()}",
        "steps:",
    ]
    for step in plan.steps:
        lines.append(f"  - id: {step.id}")
        lines.append(f"    n: {step.n}")
        lines.append(f"    primitive: {step.primitive}")
        lines.append(f"    title: {json.dumps(step.title, ensure_ascii=False)}")
        lines.append(f"    risk: {step.risk}")
        lines.append("    args:")
        for key, value in step.args.items():
            lines.append(f"      {key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def write_plan(plan: Plan, plans_dir: Path) -> Path:
    path = plans_dir / plan.id / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, render_plan_yaml(plan), mode=0o644)
    write_atomic(path.with_suffix(".json"), plan.model_dump_json(indent=2) + "\n", mode=0o644)
    return path
