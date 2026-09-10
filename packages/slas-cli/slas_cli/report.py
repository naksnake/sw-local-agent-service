"""Doctor report model and renderers: sentences for people, JSON for machines (CLAUDE.md §9)."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["ok", "warning", "blocked"]

STATUS_WORDS: dict[Status, str] = {"ok": "OK", "warning": "Warning", "blocked": "Blocked"}

EXIT_OK = 0
EXIT_BLOCKED = 2


@dataclass(frozen=True)
class ThreePartError:
    """Standard-library mirror of `slas_schemas.errors.ThreePartError` (same three fields)."""

    what_happened: str
    likely_cause: str
    what_to_do: str


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    status: Status
    sentence: str
    error: ThreePartError | None = None


@dataclass(frozen=True)
class Report:
    product: str
    data_root: str
    profile: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def blocked(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "blocked"]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "warning"]

    @property
    def exit_code(self) -> int:
        return EXIT_BLOCKED if self.blocked else EXIT_OK

    def summary(self) -> str:
        n_blocked, n_warn = len(self.blocked), len(self.warnings)
        if n_blocked:
            noun = "check" if n_blocked == 1 else "checks"
            return (
                f"Preflight found {n_blocked} blocking {noun}. Fix what is listed above and run "
                "./install.sh again; nothing was changed on this host."
            )
        if n_warn:
            noun = "warning" if n_warn == 1 else "warnings"
            return (
                f"Preflight passed with {n_warn} {noun}. The platform will run; the warnings "
                "above say what will be missing."
            )
        return "Preflight passed. This host meets every requirement."


def render_text(report: Report) -> str:
    lines = [
        f"{report.product}: preflight for the {report.profile} profile, "
        f"data root {report.data_root}",
        "",
    ]
    for check in report.checks:
        lines.append(f"{STATUS_WORDS[check.status]:8s} {check.title}: {check.sentence}")
        if check.error is not None:
            lines.append(f"         What happened: {check.error.what_happened}")
            lines.append(f"         Likely cause:  {check.error.likely_cause}")
            lines.append(f"         What to do:    {check.error.what_to_do}")
    lines.append("")
    lines.append(report.summary())
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    payload = dataclasses.asdict(report)
    payload["summary"] = report.summary()
    payload["exit_code"] = report.exit_code
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
