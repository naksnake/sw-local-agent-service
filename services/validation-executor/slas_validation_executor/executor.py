"""The Validation executor: a deterministic state machine behind the kernel's `Executor`
protocol (CLAUDE.md §10.2, INV-3). Every hardware action goes through `slas_hal`.

    lease_target · console_on · baseline_snapshot · power_cycle (ARM→QUIESCE→ACT→SETTLE→VERIFY)
    · GATE (3 consecutive boot failures abort) · sel/inventory snapshots · stress · run_diag ·
    destructive primitives (the kernel has already collected the approval) · collect_logs ·
    release_target

Run artefacts: `Validation/Runs/<ticket>/{baseline/,cycles/<n>/,cycles.json,console.log}`.
Findings from VERIFY travel on the observation; the kernel dedups them and spawns tickets.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from slas_hal.hal import Hal
from slas_hal.model import Snapshot
from slas_hal.primitives import PRIMITIVES, approval_kind
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_observability import metrics
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_schemas.finding import Finding
from slas_schemas.plan import Step
from slas_schemas.ticket import Observation
from slas_triage.dedup import finding_from_sentence
from slas_validation_executor.cycle import run_cycle
from slas_validation_executor.guardrails import Guardrails, default_guardrails
from slas_validation_executor.leases import LeaseError, LeaseTable

CycleStatus = Literal["waiting", "running", "ok", "finding", "failed", "skipped"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class CycleCell(SlasModel):
    """One cell of the LED cycle map the Validation page draws."""

    n: int = Field(ge=1)
    kind: str
    status: CycleStatus = "waiting"
    sentence: str = ""


class RunState(SlasModel):
    ticket_id: str
    target: str
    cycles: list[CycleCell] = Field(default_factory=list)
    consecutive_boot_failures: int = 0
    aborted: bool = False

    def sentence(self) -> str:
        done = sum(1 for c in self.cycles if c.status in ("ok", "finding", "failed"))
        findings = sum(1 for c in self.cycles if c.status == "finding")
        failed = sum(1 for c in self.cycles if c.status == "failed")
        head = f"{done} of {len(self.cycles)} cycles done"
        tail = []
        if findings:
            tail.append(f"{findings} with findings")
        if failed:
            tail.append(f"{failed} did not boot")
        return head + (": " + ", ".join(tail) if tail else "") + "."


class ValidationExecutor:
    def __init__(
        self,
        *,
        hal: Hal,
        data_root: Path,
        clock: Clock,
        guardrails: Guardrails | None = None,
    ) -> None:
        self.hal = hal
        self.data_root = data_root
        self.clock = clock
        self.guardrails = guardrails or default_guardrails()
        self.leases = LeaseTable(data_root / "Validation" / "leases.json")
        self._baselines: dict[str, Snapshot] = {}
        self._states: dict[str, RunState] = {}

    # --- paths and state ------------------------------------------------------------------

    def run_dir(self, ticket_id: str) -> Path:
        return self.data_root / "Validation" / "Runs" / ticket_id

    def _state(
        self, context: ExecutionContext, target: str, *, total_cycles: int, kind: str
    ) -> RunState:
        state = self._states.get(context.ticket_id)
        path = self.run_dir(context.ticket_id) / "cycles.json"
        if state is None and path.is_file():
            state = RunState.model_validate_json(path.read_text(encoding="utf-8"))
        if state is None:
            state = RunState(ticket_id=context.ticket_id, target=target)
        if not state.cycles:
            # The whole map from the first cycle on, so the page shows what is still to come.
            state.cycles = [CycleCell(n=n, kind=kind) for n in range(1, total_cycles + 1)]
        self._states[context.ticket_id] = state
        return state

    def _save_state(self, state: RunState) -> None:
        path = self.run_dir(state.ticket_id) / "cycles.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, state.model_dump_json(indent=2) + "\n", mode=0o644)

    def state_for(self, ticket_id: str) -> RunState | None:
        path = self.run_dir(ticket_id) / "cycles.json"
        if ticket_id in self._states:
            return self._states[ticket_id]
        return (
            RunState.model_validate_json(path.read_text(encoding="utf-8"))
            if path.is_file()
            else None
        )

    def _baseline(self, context: ExecutionContext) -> Snapshot:
        baseline = self._baselines.get(context.ticket_id)
        if baseline is None:
            path = self.run_dir(context.ticket_id) / "baseline" / "snapshot.json"
            if not path.is_file():
                raise RuntimeError(
                    f"{context.ticket_id} has no baseline; run baseline_snapshot first"
                )
            baseline = Snapshot.model_validate_json(path.read_text(encoding="utf-8"))
            self._baselines[context.ticket_id] = baseline
        return baseline

    # --- dispatch ---------------------------------------------------------------------------

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        if step.primitive not in PRIMITIVES:
            raise UnknownPrimitiveError(step)
        target = str(step.args["target"])
        handler = {
            "lease_target": self._lease,
            "console_on": self._console_on,
            "baseline_snapshot": self._baseline_snapshot,
            "power_cycle": self._power_cycle,
            "sel_snapshot": self._sel_snapshot,
            "inventory_snapshot": self._inventory_snapshot,
            "stress": self._tool,
            "run_diag": self._tool,
            "collect_logs": self._collect_logs,
            "release_target": self._release,
        }.get(step.primitive, self._destructive)
        return handler(step, context, target)

    # --- steps -----------------------------------------------------------------------------

    def _lease(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        try:
            lease = self.leases.acquire(
                target,
                ticket_id=context.ticket_id,
                user=context.user,
                now=self.clock.now(),
                max_hours=self.guardrails.max_run_hours,
            )
        except LeaseError as exc:
            return Observation(
                exit_code=1, summary=exc.message.what_happened, stderr=exc.message.what_to_do
            )
        return Observation(exit_code=0, summary=f"Leased {target} for this run. {lease.sentence()}")

    def _console_on(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        self.hal.console_on(target)
        return Observation(
            exit_code=0, summary=f"Serial console and syslog of {target} are being captured."
        )

    def _baseline_snapshot(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        snapshot = self.hal.snapshot(target)
        path = self.run_dir(context.ticket_id) / "baseline" / "snapshot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, snapshot.model_dump_json(indent=2) + "\n", mode=0o644)
        self._baselines[context.ticket_id] = snapshot
        return Observation(
            exit_code=0,
            summary=f"Baseline recorded. {snapshot.sentence()}",
            stdout=snapshot.model_dump_json(),
        )

    def _power_cycle(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        n = int(step.args["cycle"])
        kind = str(step.args.get("kind", "dc"))
        state = self._state(
            context, target, total_cycles=int(step.args.get("total_cycles", n)), kind=kind
        )
        if state.aborted:
            return Observation(
                exit_code=3,
                summary=f"Cycle {n} skipped: the run was aborted after repeated boot failures.",
            )
        cell = next((c for c in state.cycles if c.n == n), None)
        if cell is None:
            cell = CycleCell(n=n, kind=kind)
            state.cycles.append(cell)
            state.cycles.sort(key=lambda c: c.n)
        cell.kind = kind
        cell.status = "running"
        self._save_state(state)
        result = run_cycle(
            hal=self.hal,
            target=target,
            ticket_id=context.ticket_id,
            n=n,
            kind=kind,
            settle_s=int(step.args.get("settle_s", self.guardrails.settle_floor(kind))),
            guardrails=self.guardrails,
            baseline=self._baseline(context),
            clock=self.clock,
            run_dir=self.run_dir(context.ticket_id),
        )
        findings: list[Finding] = []
        if result.diff is not None:
            findings = [
                finding_from_sentence(
                    f.message,
                    evidence=[f"cycle {n}: {f.before} → {f.after}"],
                    severity=f.severity,
                    component=f.component,
                )
                for f in result.diff.findings
            ]
        if not result.booted:
            state.consecutive_boot_failures += 1
            cell.status = "failed"
        else:
            state.consecutive_boot_failures = 0
            cell.status = "finding" if findings else "ok"
        metrics.inc(
            "slas_validation_cycles_total",
            kind=kind,
            outcome="boot_failed" if not result.booted else "finding" if findings else "clean",
        )
        cell.sentence = result.sentence
        exit_code = 0
        summary = result.sentence
        if state.consecutive_boot_failures >= self.guardrails.consecutive_failure_abort:
            state.aborted = True
            exit_code = 3
            metrics.inc("slas_validation_runs_total", outcome="aborted")
            limit = self.guardrails.consecutive_failure_abort
            summary = (
                f"{result.sentence} That is {state.consecutive_boot_failures} boot failures "
                f"in a row; the run is aborted (guardrail consecutive_failure_abort={limit}). "
                "A person needs to look at the target."
            )
            findings.append(
                finding_from_sentence(
                    f"{target} failed to boot {state.consecutive_boot_failures} times in a row "
                    f"during {kind.upper()} cycling",
                    evidence=[c.sentence for c in state.cycles if c.status == "failed"][-3:],
                    severity="S1",
                    component="Boot",
                )
            )
        self._save_state(state)
        return Observation(
            exit_code=exit_code,
            summary=summary,
            stdout="\n".join(self.hal.console_lines(target)[-20:]),
            findings=findings,
        )

    def _sel_snapshot(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        entries = self.hal.sel(target)
        path = (
            self.run_dir(context.ticket_id)
            / "findings"
            / f"sel-{self.clock.now():%Y%m%d-%H%M%S}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            path,
            json.dumps([e.model_dump(mode="json") for e in entries], indent=2) + "\n",
            mode=0o644,
        )
        worst = max(
            (e.severity for e in entries), key=("OK", "Warning", "Critical").index, default="OK"
        )
        return Observation(
            exit_code=0,
            summary=f"SEL read: {len(entries)} entries, worst severity {worst}.",
            stdout="\n".join(e.line() for e in entries),
        )

    def _inventory_snapshot(
        self, step: Step, context: ExecutionContext, target: str
    ) -> Observation:
        inventory = self.hal.inventory(target)
        return Observation(
            exit_code=0,
            summary=(
                f"Inventory read: {inventory.model} {inventory.serial}, "
                f"{len(inventory.devices)} PCIe devices, BIOS {inventory.bios_version}, "
                f"BMC {inventory.bmc_version}."
            ),
            stdout=inventory.model_dump_json(),
        )

    def _tool(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        tool = str(step.args["tool"])
        extra = [str(a) for a in step.args.get("args", [])]
        argv = [tool, *extra]
        timeout = int(step.args.get("duration_s", step.args.get("timeout_s", 600)))
        result = self.hal.ssh(target, argv, timeout_s=timeout)
        ok = result.exit_code == 0
        return Observation(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            summary=(
                f"{tool} {'finished' if ok else 'failed'} on {target} (exit {result.exit_code})."
            ),
        )

    def _destructive(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        # The kernel has already collected the per-run approval (INV-7); the executor still
        # refuses to act on a step that is not marked destructive, whatever the plan says.
        kind = approval_kind(step.primitive, step.args)
        if step.risk != "destructive":
            message = ThreePartMessage(
                f"Step {step.n} ({step.title}) is {kind} but was not marked destructive.",
                "A destructive step must carry its risk so the kernel asks for approval first.",
                "Recompile the plan; this is a compiler defect, not something to work around.",
            )
            return Observation(
                exit_code=2, summary=message.what_happened, stderr=message.what_to_do
            )
        self.hal.fence(target, f"--- slas fence {context.ticket_id} {step.primitive} ---")
        result = self.hal.ssh(
            target,
            [step.primitive, *(f"{k}={v}" for k, v in sorted(step.args.items()) if k != "target")],
        )
        return Observation(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            summary=(
                f"{step.title}: {'done' if result.exit_code == 0 else 'failed'} on {target} "
                "(approved for this run)."
            ),
        )

    def _collect_logs(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        run_dir = self.run_dir(context.ticket_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        console = self.hal.console_lines(target)
        write_atomic(run_dir / "console.log", "\n".join(console) + "\n", mode=0o644)
        state = self.state_for(context.ticket_id)
        return Observation(
            exit_code=0,
            summary=f"Collected {len(console)} console lines"
            + (f"; {state.sentence()}" if state else "."),
            stdout="\n".join(console[-50:]),
        )

    def _release(self, step: Step, context: ExecutionContext, target: str) -> Observation:
        released = self.leases.release(target, ticket_id=context.ticket_id)
        return Observation(
            exit_code=0,
            summary=f"Released {target}."
            if released
            else f"{target} was not leased to this run; nothing to release.",
        )
