"""RUN: perform a compiled skill step by step, journalling everything (§5.6, INV-3, INV-6).

Screen steps go to the screen driver, which screenshots before and after. Everything else
goes to a `SkillExecutor` (the sandbox, SSH or HAL executor of the agent that owns the run;
a fake in tests). A destructive step is never performed without a per-run approval (INV-7).
Steps whose arguments carry a secret handle are journalled with their arguments masked.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage
from slas_schemas.plan import Step
from slas_screen.driver import ScreenDriver
from slas_screen.model import ScreenResult, ScreenStopError
from slas_skills.compiler import CompiledSkill
from slas_skills.primitives import SCREEN_PRIMITIVES
from slas_skills.templates import TemplateError, evaluate, render_value


class JournalLike(Protocol):
    def append(
        self,
        kind: Any,
        ticket_id: str,
        payload: dict[str, Any] | None = None,
        *,
        step_id: str | None = None,
    ) -> Any: ...


class StepOutcome(SlasModel):
    ok: bool
    sentence: str = Field(min_length=1)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)


class SkillExecutor(Protocol):
    def execute(self, step: Step, context: Mapping[str, Any]) -> StepOutcome: ...


class FakeSkillExecutor:
    """Scripted outcomes per step id (or per primitive); records what it was asked to do."""

    def __init__(self) -> None:
        self.outcomes: dict[str, StepOutcome] = {}
        self.calls: list[Step] = []

    def script(self, key: str, outcome: StepOutcome) -> None:
        self.outcomes[key] = outcome

    def execute(self, step: Step, context: Mapping[str, Any]) -> StepOutcome:
        self.calls.append(step)
        outcome = self.outcomes.get(step.id) or self.outcomes.get(step.primitive)
        if outcome is not None:
            return outcome
        return StepOutcome(ok=True, sentence=f"{step.title} done.", exit_code=0)


class ApprovalRequiredError(RuntimeError):
    def __init__(self, step: Step) -> None:
        self.message = ThreePartMessage(
            f"Step {step.n} ({step.title}) is destructive and has not been approved for this run.",
            "Destructive steps need a per-run human approval (INV-7); a skill never grants one.",
            "Approve the step on the ticket, then resume the run.",
        )
        super().__init__(self.message.what_happened)
        self.step = step


class StepRun(SlasModel):
    step_id: str
    n: int
    title: str
    status: str
    sentence: str
    attempts: int = 1
    screenshots: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)


class SkillRunResult(SlasModel):
    skill_id: str
    status: str
    steps: list[StepRun] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    sentence: str


def _mask(args: Mapping[str, Any], masked: bool) -> dict[str, Any]:
    if not masked:
        return dict(args)
    return dict.fromkeys(args, "[secret]")


class SkillRunner:
    def __init__(
        self,
        *,
        executor: SkillExecutor,
        journal: JournalLike,
        ticket_id: str,
        screen: ScreenDriver | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.executor = executor
        self.journal = journal
        self.ticket_id = ticket_id
        self.screen = screen
        self._sleep = sleep

    def run(
        self,
        compiled: CompiledSkill,
        *,
        approvals: set[str] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> SkillRunResult:
        approved = set(approvals or ())
        ctx: dict[str, Any] = dict(context or {})
        ctx.setdefault("steps", {})
        runs: list[StepRun] = []
        status = "done"
        for step in compiled.plan.steps:
            masked = step.id in compiled.secret_steps
            if step.when and not self._holds(step.when, ctx, step):
                runs.append(
                    StepRun(
                        step_id=step.id,
                        n=step.n,
                        title=step.title,
                        status="skipped",
                        sentence=f"Skipped: {step.when} does not hold.",
                    )
                )
                self.journal.append("note", self.ticket_id, {"skipped": step.when}, step_id=step.id)
                continue
            if step.needs_approval and step.id not in approved:
                self.journal.append(
                    "note", self.ticket_id, {"approval_required": step.title}, step_id=step.id
                )
                raise ApprovalRequiredError(step)
            run = self._perform(step, compiled, ctx, masked)
            runs.append(run)
            if run.status == "failed":
                status = "failed"
                break
            if run.status == "stopped":
                status = "stopped"
                break
        outputs = {
            name: ctx["steps"].get(step_id, {})
            for name, step_id in compiled.outputs.items()
            if step_id in ctx["steps"]
        }
        done = sum(1 for run in runs if run.status == "done")
        total = len(compiled.plan.steps)
        sentence = (
            f"{compiled.plan.summary}: {done} of {total} steps finished."
            if status == "done"
            else f"{compiled.plan.summary} {status} after step {len(runs)} of {total}."
        )
        return SkillRunResult(
            skill_id=compiled.skill_id,
            status=status,
            steps=runs,
            outputs=outputs,
            sentence=sentence,
        )

    # --- internals -----------------------------------------------------------------------

    def _holds(self, condition: str, ctx: Mapping[str, Any], step: Step) -> bool:
        try:
            return evaluate(condition, ctx, source=f"step {step.id}")
        except TemplateError:
            return False

    def _perform(
        self, step: Step, compiled: CompiledSkill, ctx: dict[str, Any], masked: bool
    ) -> StepRun:
        retries = 0
        delay = 0
        policy = compiled.on_failure
        if isinstance(policy, Mapping) and "retry" in policy:
            retries = int(policy["retry"].get("max", 0))
            delay = int(policy["retry"].get("delay_s", 0))
        attempts = 0
        screenshots: list[str] = []
        while True:
            attempts += 1
            try:
                args = render_value(step.args, ctx, source=f"step {step.id}")
            except TemplateError as exc:
                return self._failed(step, exc.message.what_happened, attempts, screenshots, ctx)
            resolved = step.model_copy(update={"args": args})
            self.journal.append(
                "intent",
                self.ticket_id,
                {"primitive": step.primitive, "args": _mask(args, masked), "attempt": attempts},
                step_id=step.id,
            )
            outcome, shots = self._dispatch(resolved, ctx)
            screenshots.extend(shots)
            self.journal.append(
                "observation",
                self.ticket_id,
                {
                    "ok": outcome.ok,
                    "sentence": outcome.sentence,
                    "exit_code": outcome.exit_code,
                    "stdout": "" if masked else outcome.stdout,
                    "stderr": "" if masked else outcome.stderr,
                    "outputs": {} if masked else outcome.outputs,
                    "screenshots": shots,
                },
                step_id=step.id,
            )
            if outcome.ok:
                ctx["steps"][step.id] = {
                    **outcome.outputs,
                    "exit_code": outcome.exit_code,
                    "stdout": outcome.stdout,
                    "stderr": outcome.stderr,
                }
                if step.primitive == "set":
                    ctx[str(args["var"])] = args["value"]
                return StepRun(
                    step_id=step.id,
                    n=step.n,
                    title=step.title,
                    status="done",
                    sentence=outcome.sentence,
                    attempts=attempts,
                    screenshots=screenshots,
                    outputs=outcome.outputs,
                )
            if attempts <= retries:
                if delay:
                    self._sleep(delay)
                continue
            return self._failed(step, outcome.sentence, attempts, screenshots, ctx, policy)

    def _failed(
        self,
        step: Step,
        sentence: str,
        attempts: int,
        screenshots: list[str],
        ctx: dict[str, Any],
        policy: Any = "stop",
    ) -> StepRun:
        if policy == "screenshot_and_stop" and self.screen is not None:
            shot = self.screen.screenshot(f"{step.id}-failure")
            if shot.after:
                screenshots.append(shot.after)
        status = "done" if policy == "continue" else "failed"
        if policy == "continue":
            ctx["steps"][step.id] = {"failed": True}
        return StepRun(
            step_id=step.id,
            n=step.n,
            title=step.title,
            status=status,
            sentence=sentence,
            attempts=attempts,
            screenshots=screenshots,
        )

    def _dispatch(self, step: Step, ctx: dict[str, Any]) -> tuple[StepOutcome, list[str]]:
        if step.primitive in SCREEN_PRIMITIVES:
            return self._screen_step(step)
        if step.primitive == "wait":
            seconds = float(step.args["seconds"])
            self._sleep(seconds)
            return StepOutcome(ok=True, sentence=f"Waited {seconds:g} seconds."), []
        if step.primitive == "set":
            return StepOutcome(ok=True, sentence=f"Set {step.args['var']}."), []
        if step.primitive == "assert":
            try:
                holds = evaluate(str(step.args["condition"]), ctx, source=f"step {step.id}")
            except TemplateError as exc:
                return StepOutcome(ok=False, sentence=exc.message.what_happened), []
            message = str(step.args["message"])
            if holds:
                return StepOutcome(ok=True, sentence=f"Checked: {message}."), []
            return StepOutcome(ok=False, sentence=f"Check failed: {message}."), []
        return self.executor.execute(step, ctx), []

    def _screen_step(self, step: Step) -> tuple[StepOutcome, list[str]]:
        if self.screen is None:
            return StepOutcome(
                ok=False,
                sentence=f"{step.title} needs a screen, and this run has no virtual display.",
            ), []
        args = step.args
        try:
            result = self._screen_call(step.primitive, args)
        except ScreenStopError as exc:
            return StepOutcome(ok=False, sentence=exc.message.what_happened), []
        shots = [path for path in (result.before, result.after) if path]
        return StepOutcome(ok=result.ok, sentence=result.sentence, outputs=result.outputs), shots

    def _screen_call(self, primitive: str, args: Mapping[str, Any]) -> ScreenResult:
        assert self.screen is not None  # noqa: S101 — checked by the caller
        screen = self.screen
        if primitive == "focus_window":
            return screen.focus_window(title=args.get("title"), wm_class=args.get("class"))
        if primitive in ("click", "double_click", "right_click"):
            count = 2 if primitive == "double_click" else 1
            button = "right" if primitive == "right_click" else "left"
            return screen.click(
                text=args.get("text"),
                image=args.get("image"),
                target=args.get("target"),
                x=args.get("x"),
                y=args.get("y"),
                button=button,
                count=count,
            )
        if primitive == "type":
            return screen.type_text(str(args["text"]))
        if primitive == "key":
            return screen.key(str(args["press"]))
        if primitive == "scroll":
            return screen.scroll(str(args["direction"]), int(args["amount"]))
        if primitive == "wait_for":
            return screen.wait_for(
                window=args.get("window"),
                text=args.get("text"),
                image=args.get("image"),
                timeout_s=float(args.get("timeout_s", 30)),
            )
        if primitive == "screenshot":
            return screen.screenshot(str(args["name"]))
        return screen.assert_visible(
            text=args.get("text"), image=args.get("image"), message=str(args["message"])
        )
