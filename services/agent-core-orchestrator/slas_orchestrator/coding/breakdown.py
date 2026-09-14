"""The task breakdown a person approves before anything runs (§9 wizard, §10.1).

A model may draft it (`Breakdowner`, behind the gateway's `planner` role); a deterministic
fallback takes the plan's own bullets. Either way the person edits and approves it in the
wizard's third step; the kernel only plans from an approved breakdown.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, model_validator

from slas_orchestrator.coding.plan_doc import PlanDocument
from slas_sandbox_manager.toolchains import Manifest, Resolution, resolve_all, toolchain_sentence
from slas_schemas.common import SlasModel

MAX_TASKS = 30
Isolation = Literal["auto", "gvisor"]
ExportTarget = Literal["zip", "remote", "bundle"]


class TaskItem(SlasModel):
    n: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    files: list[str] = Field(default_factory=list)
    acceptance: str = ""


class LanguageChoice(SlasModel):
    language: str = Field(min_length=1)
    #: Empty means "the agent picks the newest bundled toolchain and says so".
    version: str | None = None


class Breakdown(SlasModel):
    title: str = Field(min_length=1, max_length=200)
    tasks: list[TaskItem] = Field(min_length=1, max_length=MAX_TASKS)
    languages: list[LanguageChoice] = Field(min_length=1)
    isolation: Isolation = "auto"
    skills: list[str] = Field(default_factory=list)
    cross_check: bool = True
    export: ExportTarget = "zip"
    #: An opaque remote reference such as `gitlab-firmware`; never a URI (INV-5, INV-14).
    remote_ref: str | None = None
    max_iterations: int = Field(default=6, ge=1, le=20)

    @model_validator(mode="after")
    def _numbered_and_targeted(self) -> Breakdown:
        for index, task in enumerate(self.tasks, start=1):
            if task.n != index:
                raise ValueError(f"task {task.title!r} is numbered {task.n} but is the {index}th")
        if self.export == "remote" and not self.remote_ref:
            raise ValueError("exporting to a remote needs the name of a saved remote")
        return self

    def resolutions(self, manifest: Manifest) -> list[Resolution]:
        return resolve_all({c.language: c.version for c in self.languages}, manifest)

    def sentence(self, manifest: Manifest) -> str:
        """The wizard's closing sentence: what will happen, in one breath."""
        resolutions = self.resolutions(manifest)
        count = len(self.tasks)
        tools = ", ".join(f"{r.label} {r.version}" for r in resolutions)
        check = (
            "cross-check the result with 3 voters"
            if self.cross_check
            else "skip the cross-check, as you asked"
        )
        export = {
            "zip": "export a ZIP",
            "remote": f"push a branch to {self.remote_ref} for review",
            "bundle": "export a Git bundle",
        }[self.export]
        sentence = (
            f"The agent will work in an isolated sandbox with {tools}, do {count} "
            f"{'task' if count == 1 else 'tasks'}, commit on its own branch, {check}, and "
            f"{export}."
        )
        fallbacks = [r.sentence for r in resolutions if not r.honoured]
        return sentence + (" " + " ".join(fallbacks) if fallbacks else "")

    def toolchain_sentence(self, manifest: Manifest) -> str:
        return toolchain_sentence(self.resolutions(manifest))


class Breakdowner(Protocol):
    def draft(self, plan: PlanDocument) -> list[TaskItem]: ...


class FakeBreakdowner:
    def __init__(self, tasks: list[TaskItem] | None = None) -> None:
        self.tasks = tasks
        self.calls: list[str] = []

    def draft(self, plan: PlanDocument) -> list[TaskItem]:
        self.calls.append(plan.title)
        return list(self.tasks) if self.tasks is not None else tasks_from_plan(plan)


def tasks_from_plan(plan: PlanDocument) -> list[TaskItem]:
    """Deterministic fallback: one task per bullet; a plan with no bullets is one task."""
    titles = plan.tasks[:MAX_TASKS] or [plan.title]
    return [TaskItem(n=n, title=title[:200]) for n, title in enumerate(titles, start=1)]


def propose(plan: PlanDocument, *, breakdowner: Breakdowner | None = None) -> Breakdown:
    tasks = breakdowner.draft(plan) if breakdowner is not None else tasks_from_plan(plan)
    if not tasks:
        tasks = tasks_from_plan(plan)
    languages = [LanguageChoice(language=lang) for lang in plan.languages] or [
        LanguageChoice(language="shell")
    ]
    return Breakdown(
        title=plan.title,
        tasks=[task.model_copy(update={"n": n}) for n, task in enumerate(tasks, start=1)],
        languages=languages,
    )
