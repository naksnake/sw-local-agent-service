"""INGEST for the Coding Agent: `plan.md` → title, task lines, detected languages (§10.1)."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from pydantic import Field

from slas_sandbox_manager.toolchains import detect_languages
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_TASK = re.compile(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")
MAX_PLAN_BYTES = 256 * 1024


class PlanError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class PlanDocument(SlasModel):
    title: str = Field(min_length=1, max_length=200)
    tasks: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    text: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def job_id(self) -> str:
        return f"job-{self.sha256[:12]}"

    def sentence(self) -> str:
        count = len(self.tasks)
        langs = ", ".join(self.languages) if self.languages else "no language we recognise"
        return (
            f"{self.title}: {count} {'task' if count == 1 else 'tasks'}; the plan mentions {langs}."
        )


def parse_plan(text: str, *, filename: str = "plan.md") -> PlanDocument:
    if not text.strip():
        raise PlanError(
            ThreePartMessage(
                f"{filename} is empty.",
                "A coding task starts from a plan with a title and a list of tasks.",
                "Write at least one task as a bullet, then upload the plan again.",
            )
        )
    if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
        raise PlanError(
            ThreePartMessage(
                f"{filename} is larger than {MAX_PLAN_BYTES // 1024} KB.",
                "A plan is a short document; code and data belong in the project.",
                "Shorten the plan or split the work into several tasks.",
            )
        )
    heading = _HEADING.search(text)
    title = heading.group(1) if heading else PurePosixPath(filename).stem.replace("-", " ").strip()
    tasks: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _TASK.match(line)
        if match:
            tasks.append(match.group(1))
    return PlanDocument(
        title=(title or "Untitled plan")[:200],
        tasks=tasks,
        languages=detect_languages(text),
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
