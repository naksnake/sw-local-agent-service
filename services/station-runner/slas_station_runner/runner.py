"""The station runner: performs verified batches on the station's own screen and shell.

    skill    the compiled GUI steps through the local ScreenDriver (screenshot before/after);
             secret handles are substituted at type time and never journalled
    command  one argv from the station's allowlist, no shell
    state    config files, recent logs and application versions for the backup

The runner owns the display (Xvfb or the station's console); the platform only ever sees
what comes back here (INV-4).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from slas_hal.drivers.process import ProcessRunner
from slas_hal.hal import CommandResult
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Step
from slas_screen.driver import ScreenDriver
from slas_skills.compiler import SECRET_PREFIX, CompiledSkill
from slas_skills.runner import SkillExecutor, SkillRunner, StepOutcome
from slas_station_runner.protocol import (
    BatchError,
    BatchResult,
    Screenshot,
    SignedBatch,
    StateSnapshot,
    StepBatch,
    verify_batch,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class StationConfig(SlasModel):
    station: str = Field(min_length=1)
    #: Programs a `command` batch or a skill `run` step may start; anything else is refused.
    allowed_programs: list[str] = Field(default_factory=list)
    #: Files the backup collects (config, recent logs) and the command that reports versions.
    state_files: list[str] = Field(default_factory=list)
    versions_command: list[str] = Field(default_factory=list)


class RunnerJournal:
    """One JSONL per station; never carries a secret (the skill runner masks secret steps)."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock

    def append(
        self,
        kind: Any,
        ticket_id: str,
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": self.clock.now().isoformat(),
            "kind": str(kind),
            "ticket_id": ticket_id,
            "step_id": step_id,
            "payload": payload or {},
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


class StationLocalExecutor:
    """Non-screen skill steps on the station: `run` (argv, allowlisted); the rest refused."""

    def __init__(self, config: StationConfig, processes: ProcessRunner) -> None:
        self.config = config
        self.processes = processes
        self.calls: list[list[str]] = []

    def execute(self, step: Step, context: Mapping[str, Any]) -> StepOutcome:
        if step.primitive in ("run", "ssh"):
            argv = [str(a) for a in step.args.get("command", [])]
            if not argv or argv[0] not in self.config.allowed_programs:
                return StepOutcome(
                    ok=False,
                    sentence=(
                        f"{argv[0] if argv else 'an empty command'} is not on this station's "
                        "allowlist; nothing was run."
                    ),
                    exit_code=126,
                )
            self.calls.append(argv)
            result = self.processes.run(
                argv, env={}, timeout_s=int(step.args.get("timeout_s", 600))
            )
            expected = int(step.args.get("expect_exit", 0))
            return StepOutcome(
                ok=result.exit_code == expected,
                sentence=f"{argv[0]} exited {result.exit_code}.",
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return StepOutcome(
            ok=False,
            sentence=f"{step.primitive} steps are not performed on a station.",
            exit_code=126,
        )


def _substitute(value: Any, secrets: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return secrets.get(value, value) if value.startswith(SECRET_PREFIX) else value
    if isinstance(value, list):
        return [_substitute(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, secrets) for k, v in value.items()}
    return value


class StationRunner:
    def __init__(
        self,
        config: StationConfig,
        *,
        keys: Mapping[str, bytes],
        screen: ScreenDriver,
        processes: ProcessRunner,
        clock: Clock,
        state_dir: Path,
    ) -> None:
        self.config = config
        self.keys = dict(keys)
        self.screen = screen
        self.processes = processes
        self.clock = clock
        self.state_dir = state_dir
        self.journal = RunnerJournal(state_dir / "runner-journal.jsonl", clock)
        self.local: SkillExecutor = StationLocalExecutor(config, processes)
        self.seen: set[str] = set()
        self.handled: list[str] = []

    # --- entry ------------------------------------------------------------------------------

    def handle(self, signed: SignedBatch) -> BatchResult:
        batch = verify_batch(
            signed,
            keys=self.keys,
            station=self.config.station,
            now=self.clock.now(),
            seen=self.seen,
        )
        self.handled.append(batch.batch_id)
        self.journal.append(
            "batch",
            batch.ticket_id,
            {"batch_id": batch.batch_id, "kind": batch.kind, "key_id": signed.key_id},
        )
        if batch.kind == "skill":
            return self._skill(batch)
        if batch.kind == "command":
            return self._command(batch)
        return self._state(batch)

    # --- kinds ------------------------------------------------------------------------------

    def _skill(self, batch: StepBatch) -> BatchResult:
        if batch.compiled is None:
            raise BatchError(
                ThreePartMessage(
                    "The skill batch carries no compiled steps.",
                    "The executor sent kind=skill without a plan.",
                    "Nothing was performed. This is an executor defect.",
                )
            )
        compiled = self._with_secrets(batch.compiled, batch.secrets)
        runner = SkillRunner(
            executor=self.local,
            journal=self.journal,
            ticket_id=batch.ticket_id,
            screen=self.screen,
            sleep=lambda _s: None,
        )
        result = runner.run(compiled, approvals=set(batch.approvals), context={})
        shots = [
            Screenshot(name=Path(path).name, png_base64=self._png(path))
            for run in result.steps
            for path in run.screenshots
        ]
        return BatchResult(
            batch_id=batch.batch_id,
            kind="skill",
            ok=result.status == "done",
            sentence=result.sentence,
            skill=result,
            screenshots=shots,
        )

    def _with_secrets(self, compiled: CompiledSkill, secrets: Mapping[str, str]) -> CompiledSkill:
        """Substitute handles in the steps the compiler marked secret; the journal masks them."""
        if not secrets:
            return compiled
        steps = [
            step.model_copy(update={"args": _substitute(step.args, secrets)})
            if step.id in compiled.secret_steps
            else step
            for step in compiled.plan.steps
        ]
        return compiled.model_copy(
            update={"plan": compiled.plan.model_copy(update={"steps": steps})}
        )

    def _command(self, batch: StepBatch) -> BatchResult:
        argv = list(batch.command)
        if not argv or argv[0] not in self.config.allowed_programs:
            raise BatchError(
                ThreePartMessage(
                    f"{argv[0] if argv else 'An empty command'} is not on {self.config.station}'s "
                    "allowlist.",
                    "A station runs only the programs its configuration names; nothing else, "
                    "and never a shell.",
                    "Add the program to the station's allowlist if it belongs there.",
                )
            )
        result = self.processes.run(argv, env={}, timeout_s=batch.timeout_s)
        self.journal.append(
            "command", batch.ticket_id, {"argv": argv, "exit_code": result.exit_code}
        )
        return BatchResult(
            batch_id=batch.batch_id,
            kind="command",
            ok=result.exit_code == 0,
            sentence=f"{argv[0]} exited {result.exit_code} on {self.config.station}.",
            command=result,
        )

    def _state(self, batch: StepBatch) -> BatchResult:
        snapshot = self.collect_state()
        return BatchResult(
            batch_id=batch.batch_id,
            kind="state",
            ok=True,
            sentence=(
                f"Collected {len(snapshot.files)} files and {len(snapshot.versions)} "
                f"versions from {self.config.station}."
            ),
            state=snapshot,
        )

    def collect_state(self) -> StateSnapshot:
        files: dict[str, str] = {}
        for name in self.config.state_files:
            path = Path(name)
            try:
                files[name] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                files[name] = f"(unreadable: {name})"
        versions: dict[str, str] = {}
        if self.config.versions_command:
            result = self.processes.run(self.config.versions_command, env={}, timeout_s=60)
            versions = _parse_versions(result)
        return StateSnapshot(files=files, versions=versions, taken_at=self.clock.now())

    # --- helpers -------------------------------------------------------------------------------

    @staticmethod
    def _png(path: str) -> str:
        try:
            return base64.b64encode(Path(path).read_bytes()).decode("ascii")
        except OSError:
            return base64.b64encode(b"").decode("ascii") or "AA=="


def _parse_versions(result: CommandResult) -> dict[str, str]:
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return {"raw": result.stdout.strip()[:2000]}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {"raw": result.stdout.strip()[:2000]}


def allowlisted(config: StationConfig, argv: Sequence[str]) -> bool:
    return bool(argv) and argv[0] in config.allowed_programs
