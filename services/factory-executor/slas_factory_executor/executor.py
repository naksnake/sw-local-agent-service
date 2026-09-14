"""The Factory executor (CLAUDE.md §10.3 ACT/VERDICT/BACKUP, INV-3, INV-11): a deterministic
loop on a station through the station runner, behind the kernel's `Executor` protocol.

    lease_station → station_command → skill (GUI, screenshots back) → wait_for_screen →
    read_result → read_sensors → check_event_log → verdict → backup_station → release_station

The verdict: the deterministic gate first (result PASS, sensors within limits, event log
empty). Only a unit that passes the gate is put to the voters, and PASS needs 3 of 3. A gate
failure is FAIL; fewer than 3 votes is the line lead's call. In both cases the step fails, so
the kernel stops before `release_station`: the unit stays on, the station stays leased, and
the finding on the observation becomes the draft ticket for the line lead.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field

from slas_factory_executor.backup import backup_sentence, write_backup
from slas_factory_executor.mes import Verdict
from slas_factory_executor.primitives import PRIMITIVES
from slas_hal.credentials import CredentialResolver
from slas_kernel.executor import ExecutionContext, UnknownPrimitiveError
from slas_kernel.leases import LeaseError, LeaseTable
from slas_kernel.rca import CrossChecker
from slas_kernel.skills import compiled_from_step
from slas_observability import metrics
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_schemas.finding import Finding
from slas_schemas.plan import Plan, Step
from slas_schemas.ticket import Observation
from slas_skills.compiler import CompiledSkill
from slas_station_runner.protocol import (
    BatchError,
    BatchResult,
    SignedBatch,
    StepBatch,
    new_batch_id,
    sign_batch,
)
from slas_station_runner.server import RunnerClient
from slas_triage.dedup import finding_from_sentence

CellStatus = Literal["waiting", "running", "ok", "failed", "skipped"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class StepCell(SlasModel):
    """One cell of the test-step map the Factory page draws."""

    n: int = Field(ge=1)
    title: str
    status: CellStatus = "waiting"
    sentence: str = ""
    screenshots: list[str] = Field(default_factory=list)


class JobState(SlasModel):
    ticket_id: str
    station: str
    unit_sn: str = ""
    mes_ticket_no: str = ""
    cells: list[StepCell] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    sensors: dict[str, Any] = Field(default_factory=dict)
    sensor_problems: list[str] = Field(default_factory=list)
    event_log: list[str] = Field(default_factory=list)
    verdict: Verdict | None = None
    verdict_sentence: str = ""
    votes_sentence: str = ""
    held: bool = False
    hold_reason: str = ""
    decided_by: str = ""

    def sentence(self) -> str:
        done = sum(1 for c in self.cells if c.status in ("ok", "failed"))
        head = f"{done} of {len(self.cells)} steps done"
        if self.verdict == "PASS":
            return f"{head}. Verdict: PASS ({self.decided_by})."
        if self.verdict == "FAIL":
            return f"{head}. Verdict: FAIL; the station is held for the line lead."
        if self.verdict == "line_lead":
            return f"{head}. The voters did not agree; the line lead decides. The station is held."
        return head + "."


class FactoryExecutor:
    def __init__(
        self,
        *,
        runners: Mapping[str, RunnerClient],
        resolver: CredentialResolver,
        signing_key_ref: str,
        signing_key_id: str,
        data_root: Path,
        clock: Clock,
        cross_checker: CrossChecker | None = None,
        lease_hours: int = 8,
        batch_ttl_s: int = 300,
        sleep: Callable[[float], None] | None = None,
        station_keys: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        self.runners = dict(runners)
        self.resolver = resolver
        self.signing_key_ref = signing_key_ref
        self.signing_key_id = signing_key_id
        #: Per-station (key_ref, key_id) from enrolment (P10); a station without one uses the
        #: line-wide key above. Refs resolve at dispatch; the key never lands in a plan or log.
        self.station_keys = dict(station_keys or {})
        self.data_root = data_root
        self.clock = clock
        self.cross_checker = cross_checker
        self.lease_hours = lease_hours
        self.batch_ttl_s = batch_ttl_s
        self.leases = LeaseTable(data_root / "Factory" / "leases.json", noun="station")
        self._states: dict[str, JobState] = {}
        self._batches = 0

    # --- paths and state ----------------------------------------------------------------------

    def job_dir(self, ticket_id: str) -> Path:
        return self.data_root / "Factory" / "Jobs" / ticket_id

    def state_for(self, ticket_id: str) -> JobState | None:
        if ticket_id in self._states:
            return self._states[ticket_id]
        path = self.job_dir(ticket_id) / "state.json"
        if path.is_file():
            state = JobState.model_validate_json(path.read_text(encoding="utf-8"))
            self._states[ticket_id] = state
            return state
        return None

    def _state(self, context: ExecutionContext, step: Step) -> JobState:
        state = self.state_for(context.ticket_id)
        if state is None:
            state = JobState(ticket_id=context.ticket_id, station=str(step.args["station"]))
            self._states[context.ticket_id] = state
        total = int(step.args.get("loop_steps", step.n))
        if len(state.cells) < total:
            state.cells = [
                *state.cells,
                *(StepCell(n=n, title=f"Step {n}") for n in range(len(state.cells) + 1, total + 1)),
            ]
        cell = state.cells[step.n - 1]
        cell.title = step.title
        cell.status = "running"
        self._save(state)
        return state

    def _save(self, state: JobState) -> None:
        path = self.job_dir(state.ticket_id) / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, state.model_dump_json(indent=2) + "\n", mode=0o644)

    def _finish(self, state: JobState, step: Step, observation: Observation) -> Observation:
        cell = state.cells[step.n - 1]
        cell.status = "ok" if observation.exit_code == 0 else "failed"
        cell.sentence = observation.summary
        cell.screenshots = list(observation.screenshots)
        self._save(state)
        return observation

    # --- dispatch -------------------------------------------------------------------------------

    def execute(self, step: Step, context: ExecutionContext) -> Observation:
        if step.primitive not in PRIMITIVES:
            raise UnknownPrimitiveError(step)
        state = self._state(context, step)
        handler = {
            "lease_station": self._lease,
            "station_command": self._command,
            "skill": self._skill,
            "wait_for_screen": self._wait_for_screen,
            "read_result": self._read_result,
            "read_sensors": self._read_sensors,
            "check_event_log": self._check_event_log,
            "verdict": self._verdict,
            "backup_station": self._backup,
            "release_station": self._release,
            "station_config_change": self._config_change,
        }[step.primitive]
        try:
            observation = handler(step, context, state)
        except BatchError as exc:
            observation = Observation(
                exit_code=2, summary=exc.message.what_happened, stderr=exc.message.render()
            )
        return self._finish(state, step, observation)

    # --- the runner ----------------------------------------------------------------------------

    def _runner(self, station: str) -> RunnerClient:
        try:
            return self.runners[station]
        except KeyError:
            raise BatchError(
                ThreePartMessage(
                    f"No station runner is configured for {station}.",
                    "The factory executor only knows the stations in its runner table.",
                    "Register the station's runner address and certificate, then start again.",
                )
            ) from None

    def _sign(self, batch: StepBatch) -> SignedBatch:
        key_ref, key_id = self.station_keys.get(
            batch.station, (self.signing_key_ref, self.signing_key_id)
        )
        key = self.resolver.resolve(key_ref).encode("utf-8")
        return sign_batch(batch, key_id=key_id, key=key)

    def _send(self, context: ExecutionContext, step: Step, batch: StepBatch) -> BatchResult:
        self._batches += 1
        try:
            result = self._runner(batch.station).send(self._sign(batch))
        except BatchError:
            metrics.inc(
                "slas_station_batches_total", station=batch.station, kind=batch.kind, ok="false"
            )
            raise
        metrics.inc(
            "slas_station_batches_total",
            station=batch.station,
            kind=batch.kind,
            ok="true" if result.ok else "false",
        )
        self._journal(
            context.ticket_id,
            {"step": step.id, "batch": batch.batch_id, "kind": batch.kind, "ok": result.ok},
        )
        return result

    def _batch(self, context: ExecutionContext, step: Step, **fields: Any) -> StepBatch:
        return StepBatch(
            batch_id=new_batch_id(context.ticket_id, step.id, self._batches + 1),
            ticket_id=context.ticket_id,
            station=str(step.args["station"]),
            issued_at=self.clock.now(),
            ttl_s=self.batch_ttl_s,
            **fields,
        )

    def _journal(self, ticket_id: str, entry: dict[str, object]) -> None:
        path = self.job_dir(ticket_id) / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": self.clock.now().isoformat(), **entry}) + "\n")

    def _store_screens(self, ticket_id: str, step_id: str, result: BatchResult) -> list[str]:
        paths: list[str] = []
        screens = self.job_dir(ticket_id) / "screens"
        screens.mkdir(parents=True, exist_ok=True)
        for shot in result.screenshots:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", shot.name)
            path = screens / f"{step_id}-{safe}"
            path.write_bytes(base64.b64decode(shot.png_base64))
            paths.append(str(path))
        return paths

    # --- steps ----------------------------------------------------------------------------------

    def _lease(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        station = str(step.args["station"])
        try:
            lease = self.leases.acquire(
                station,
                ticket_id=context.ticket_id,
                user=context.user,
                now=self.clock.now(),
                max_hours=self.lease_hours,
            )
        except LeaseError as exc:
            return Observation(
                exit_code=1, summary=exc.message.what_happened, stderr=exc.message.what_to_do
            )
        state.unit_sn = str(step.args.get("unit_sn", ""))
        state.mes_ticket_no = str(step.args.get("mes_ticket_no", ""))
        return Observation(
            exit_code=0,
            summary=f"Leased {station} for unit {state.unit_sn}. {lease.sentence()}",
        )

    def _command(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        argv = [str(a) for a in step.args["command"]]
        result = self._send(
            context,
            step,
            self._batch(
                context,
                step,
                kind="command",
                command=argv,
                timeout_s=int(step.args.get("timeout_s", 600)),
            ),
        )
        command = result.command
        exit_code = command.exit_code if command else 1
        expected = int(step.args.get("expect_exit", 0))
        return Observation(
            exit_code=0 if exit_code == expected else exit_code or 1,
            stdout=command.stdout if command else "",
            stderr=command.stderr if command else "",
            summary=result.sentence,
        )

    def _skill(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        # The kernel compiled the skill at PLAN, after the enablement gate (ADR-0013). This
        # executor only resolves the secret handles for this batch and sends the steps.
        compiled = compiled_from_step(step)
        if compiled is None:
            message = ThreePartMessage(
                f"Step {step.id} reached the station without compiled skill steps.",
                "The kernel's skill gate did not run for this plan, so the skill "
                f"{step.args.get('skill_id', '(unnamed)')} was never expanded.",
                "Give the kernel a skill gate; the Factory executor never compiles a skill itself.",
            )
            return Observation(exit_code=2, summary=message.what_happened, stderr=message.render())
        secret_refs: dict[str, str] = dict(step.args.get("secret_refs", {}))
        secrets = {
            compiled.secret_handles[name]: self.resolver.resolve(ref)
            for name, ref in secret_refs.items()
            if name in compiled.secret_handles
        }
        result = self._send(
            context,
            step,
            self._batch(context, step, kind="skill", compiled=compiled, secrets=secrets),
        )
        shots = self._store_screens(context.ticket_id, step.id, result)
        run = result.skill
        lines = [f"{r.n}. {r.title}: {r.sentence}" for r in (run.steps if run else [])]
        return Observation(
            exit_code=0 if result.ok else 1,
            summary=result.sentence,
            stdout="\n".join(lines),
            screenshots=shots,
        )

    def _single_step_skill(self, step: Step, primitive: str, args: dict[str, Any]) -> CompiledSkill:
        """One inline GUI step (`wait_for_screen`) as a batch: not a skill, no expansion."""
        plan = Plan(
            id=f"plan-{step.id}",
            job_id="inline",
            summary=step.title,
            created_at=self.clock.now(),
            steps=[Step(id=step.id, n=1, primitive=primitive, title=step.title, args=args)],
        )
        return CompiledSkill(skill_id=f"inline-{primitive}", plan=plan)

    def _wait_for_screen(
        self, step: Step, context: ExecutionContext, state: JobState
    ) -> Observation:
        compiled = self._single_step_skill(
            step,
            "wait_for",
            {"text": str(step.args["text"]), "timeout_s": int(step.args.get("timeout_s", 300))},
        )
        result = self._send(
            context, step, self._batch(context, step, kind="skill", compiled=compiled)
        )
        shots = self._store_screens(context.ticket_id, step.id, result)
        run = result.skill
        sentence = run.steps[0].sentence if run and run.steps else result.sentence
        return Observation(exit_code=0 if result.ok else 1, summary=sentence, screenshots=shots)

    def _read_json(self, step: Step, context: ExecutionContext) -> tuple[dict[str, Any], str, int]:
        argv = [str(a) for a in step.args["command"]]
        result = self._send(context, step, self._batch(context, step, kind="command", command=argv))
        command = result.command
        text = command.stdout if command else ""
        code = command.exit_code if command else 1
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        return (data if isinstance(data, dict) else {}), text, code

    def _read_result(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        data, text, code = self._read_json(step, context)
        if code != 0 or "result" not in data:
            return Observation(
                exit_code=1,
                stdout=text,
                summary=(
                    f"The vendor tool reported no result (exit {code}); the unit cannot be judged."
                ),
            )
        state.result = data
        failed = [
            name
            for name, value in dict(data.get("tests", {})).items()
            if str(value).upper() != "PASS"
        ]
        summary = f"BurnIn reports {data['result']}"
        summary += f"; failed: {', '.join(failed)}." if failed else "; every test passed."
        return Observation(exit_code=0, stdout=text, summary=summary)

    def _read_sensors(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        data, text, code = self._read_json(step, context)
        if code != 0:
            return Observation(
                exit_code=1, stdout=text, summary=f"The sensors could not be read (exit {code})."
            )
        limits = {str(k): float(v) for k, v in dict(step.args.get("limits", {})).items()}
        problems = [
            f"{name} is {data[name]}, above the limit of {limit:g}"
            for name, limit in limits.items()
            if isinstance(data.get(name), int | float) and float(data[name]) > limit
        ]
        state.sensors = data
        state.sensor_problems = problems
        readings = ", ".join(f"{k} {v}" for k, v in data.items())
        summary = f"Sensors: {readings}." + (
            f" Out of limits: {'; '.join(problems)}." if problems else " All within limits."
        )
        return Observation(exit_code=0, stdout=text, summary=summary)

    def _check_event_log(
        self, step: Step, context: ExecutionContext, state: JobState
    ) -> Observation:
        argv = [str(a) for a in step.args["command"]]
        result = self._send(context, step, self._batch(context, step, kind="command", command=argv))
        command = result.command
        lines = [line for line in (command.stdout if command else "").splitlines() if line.strip()]
        state.event_log = lines
        if command is None or command.exit_code != 0:
            return Observation(exit_code=1, summary="The event log could not be read.")
        summary = (
            "The event log is empty."
            if not lines
            else (
                f"The event log has {len(lines)} {'entry' if len(lines) == 1 else 'entries'}: "
                f"{lines[0]}"
            )
        )
        return Observation(exit_code=0, stdout="\n".join(lines), summary=summary)

    def _verdict(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        reasons: list[str] = []
        result = str(state.result.get("result", "")).upper()
        if result != "PASS":
            failed = [
                n
                for n, v in dict(state.result.get("tests", {})).items()
                if str(v).upper() != "PASS"
            ]
            reasons.append(
                f"BurnIn reported {result or 'no result'}"
                + (f" ({', '.join(failed)})" if failed else "")
            )
        reasons.extend(state.sensor_problems)
        if state.event_log:
            reasons.append(f"the event log is not empty ({state.event_log[0]})")
        unit = state.unit_sn or "the unit"
        if reasons:
            return self._hold(
                state,
                "FAIL",
                f"Unit {unit} failed the final test on {state.station}: {'; '.join(reasons)}",
                decided_by="the deterministic gate",
            )

        evidence = [
            f"Unit {unit} on {state.station}",
            f"BurnIn result: {json.dumps(state.result, ensure_ascii=False)}",
            f"Sensors: {json.dumps(state.sensors, ensure_ascii=False)} (all within limits)",
            "Event log: empty",
        ]
        if self.cross_checker is None:
            return self._hold(
                state,
                "line_lead",
                f"Unit {unit} passed every check on {state.station}, but no voters are configured",
                decided_by="nobody yet",
                extra="Not cross-checked: no voters are configured, so the line lead decides.",
            )
        verdict = self.cross_checker.cross_check("factory_pass", evidence)
        passes = sum(1 for v in verdict.votes if v.verdict == "approve")
        state.votes_sentence = verdict.sentence
        if verdict.agreed and passes >= 3 and len(verdict.votes) >= 3:
            state.verdict = "PASS"
            state.decided_by = f"{passes} of {len(verdict.votes)} voters"
            metrics.inc("slas_factory_verdicts_total", verdict="PASS", decided_by="voters")
            state.verdict_sentence = (
                f"PASS: {passes} of {len(verdict.votes)} voters say PASS. {verdict.sentence}"
            )
            return Observation(
                exit_code=0, summary=state.verdict_sentence, votes=list(verdict.votes)
            )
        concerns = (
            "; ".join(verdict.concerns) if verdict.concerns else "no concern was written down"
        )
        return self._hold(
            state,
            "line_lead",
            f"Unit {unit} passed every check on {state.station}, but only {passes} of "
            f"{len(verdict.votes)} voters say PASS ({concerns})",
            decided_by="nobody yet",
            votes=list(verdict.votes),
            extra=verdict.sentence,
        )

    def _hold(
        self,
        state: JobState,
        verdict: Verdict,
        reason: str,
        *,
        decided_by: str,
        votes: list[Any] | None = None,
        extra: str = "",
    ) -> Observation:
        state.verdict = verdict
        state.held = True
        state.hold_reason = reason
        state.decided_by = decided_by
        metrics.inc(
            "slas_factory_verdicts_total",
            verdict=verdict,
            decided_by="gate" if verdict == "FAIL" else "pending",
        )
        metrics.add_gauge("slas_factory_stations_held", 1)
        head = "FAIL" if verdict == "FAIL" else "The line lead decides"
        state.verdict_sentence = (
            f"{head}: {reason}. The unit stays on and {state.station} is held; a ticket is drafted "
            f"for the line lead." + (f" {extra}" if extra else "")
        )
        finding: Finding = finding_from_sentence(
            reason
            if verdict == "FAIL"
            else f"{reason}; line lead decision needed on {state.station}",
            evidence=[
                f"result: {json.dumps(state.result, ensure_ascii=False)}",
                f"sensors: {json.dumps(state.sensors, ensure_ascii=False)}",
                f"event log: {' | '.join(state.event_log) or 'empty'}",
            ],
            severity="S2" if verdict == "FAIL" else "S3",
            component="Final test",
        )
        return Observation(
            exit_code=1, summary=state.verdict_sentence, findings=[finding], votes=list(votes or [])
        )

    def _backup(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        result = self._send(context, step, self._batch(context, step, kind="state"))
        if result.state is None:
            return Observation(exit_code=1, summary="The station returned no state to back up.")
        export = write_backup(
            self.data_root,
            station=state.station,
            ticket_id=context.ticket_id,
            snapshot=result.state,
            now=self.clock.now(),
        )
        return Observation(
            exit_code=0, summary=backup_sentence(result.state, export), exports=[export]
        )

    def _release(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        released = self.leases.release(str(step.args["station"]), ticket_id=context.ticket_id)
        state.held = False
        return Observation(
            exit_code=0,
            summary=f"Released {state.station}."
            if released
            else f"{state.station} was not leased to this job.",
        )

    def _config_change(self, step: Step, context: ExecutionContext, state: JobState) -> Observation:
        if step.risk != "destructive":
            message = ThreePartMessage(
                f"Step {step.n} ({step.title}) changes a station setting but is not marked "
                "destructive.",
                "A station configuration change needs a per-run approval (INV-7).",
                "Recompile the template; this is a compiler defect, not something to work around.",
            )
            return Observation(
                exit_code=2, summary=message.what_happened, stderr=message.what_to_do
            )
        argv = ["station-ctl", "set", str(step.args["setting"]), str(step.args["value"])]
        result = self._send(context, step, self._batch(context, step, kind="command", command=argv))
        return Observation(
            exit_code=0 if result.ok else 1,
            summary=f"{step.title}: {'done' if result.ok else 'failed'} (approved for this job).",
        )

    # --- the operator: watch and take over (P10) ------------------------------------------

    def control(
        self, ticket_id: str, verb: Literal["pause", "resume", "abort", "status"], *, by: str
    ) -> BatchResult:
        """Pause, resume or abort the runner on the job's station; the operator drives the
        station through the VNC view in between. Recorded in the job's journal."""
        state = self.state_for(ticket_id)
        if state is None:
            raise KeyError(ticket_id)
        self._batches += 1
        batch = StepBatch(
            batch_id=new_batch_id(ticket_id, f"control-{verb}", self._batches),
            ticket_id=ticket_id,
            station=state.station,
            issued_at=self.clock.now(),
            ttl_s=self.batch_ttl_s,
            kind="control",
            command=[verb, by],
        )
        result = self._runner(state.station).send(self._sign(batch))
        self._journal(ticket_id, {"control": verb, "by": by, "sentence": result.sentence})
        return result

    # --- the line lead --------------------------------------------------------------------------

    def decide(
        self, ticket_id: str, *, verdict: Literal["PASS", "FAIL"], by: str, note: str
    ) -> JobState:
        """The line lead's decision on a held unit; releases the station. Recorded, never
        automatic (INV-7: a PASS/FAIL override is a person's act)."""
        state = self.state_for(ticket_id)
        if state is None:
            raise KeyError(ticket_id)
        state.verdict = verdict
        state.decided_by = f"{by} (line lead)"
        if state.held:
            metrics.add_gauge("slas_factory_stations_held", -1)
        state.held = False
        metrics.inc("slas_factory_verdicts_total", verdict=verdict, decided_by="line_lead")
        state.verdict_sentence = f"{verdict}: decided by {by}. {note}".strip()
        self.leases.release(state.station, ticket_id=ticket_id)
        self._journal(ticket_id, {"line_lead": by, "verdict": verdict, "note": note})
        self._save(state)
        return state
