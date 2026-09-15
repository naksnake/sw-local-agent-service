"""Render preflight results as plain language (text) or as data (JSON).

The text report is what an engineer reads on a fresh host: one line per check, a
three-part explanation under anything that needs attention, and one summary sentence.
The JSON form is for scripts and CI. Neither contains a code an engineer has to look up.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from slas_cli.doctor.checks import CheckResult, DoctorSettings, HostFacts
from slas_kernel.branding import PRODUCT_NAME

_MARKS_UNICODE = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}
_MARKS_ASCII = {"ok": "+", "warn": "!", "fail": "x", "skip": "-"}


@dataclass(frozen=True, slots=True)
class Summary:
    ok: int
    warn: int
    fail: int
    skip: int

    @property
    def ready(self) -> bool:
        return self.fail == 0

    def sentence(self) -> str:
        problems = _plural(self.fail, "problem")
        attention = _plural(self.warn, "item")
        if self.fail == 0 and self.warn == 0:
            return "Everything looks ready. You can install."
        if self.fail == 0:
            verb = "is" if self.warn == 1 else "are"
            return f"Ready to install. {attention} {verb} worth a look first."
        sentence = f"{problems} must be fixed before installing."
        if self.warn:
            more = "1 more item is" if self.warn == 1 else f"{self.warn} more items are"
            sentence += f" {more} worth a look."
        return sentence


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular if count == 1 else singular + 's'}"


def summarize(results: Sequence[CheckResult]) -> Summary:
    counts = dict.fromkeys(("ok", "warn", "fail", "skip"), 0)
    for result in results:
        counts[result.status] += 1
    return Summary(**counts)


def exit_code(results: Sequence[CheckResult]) -> int:
    """0 when nothing blocks the install, 1 when at least one check failed."""
    return 0 if summarize(results).ready else 1


def supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "✓✗—·".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def render_text(
    results: Sequence[CheckResult],
    facts: HostFacts,
    settings: DoctorSettings,
    *,
    unicode: bool = True,
) -> str:
    marks = _MARKS_UNICODE if unicode else _MARKS_ASCII
    dash = "—" if unicode else "-"
    dot = " · " if unicode else " | "
    width = max((len(result.title) for result in results), default=0)
    indent = " " * (4 + width + 2)

    lines = [
        f"{PRODUCT_NAME} {dash} preflight",
        "Checks this host only. Nothing is sent anywhere.",
        f"Host: {facts.sentence(dot)}",
        f"Profile: {settings.profile}{dot}Data root: {settings.data_root}",
        "",
    ]
    for result in results:
        lines.append(f"  {marks[result.status]} {result.title.ljust(width)}  {result.summary}")
        if result.detail is not None:
            lines.append(f"{indent}Likely cause: {result.detail.likely_cause}")
            lines.append(f"{indent}What to do: {result.detail.what_to_do}")
    summary = summarize(results)
    lines.extend(["", f"Summary: {summary.sentence()}"])
    if not summary.ready:
        lines.append("Fix the problems above and run ./install.sh again.")
    return "\n".join(lines) + "\n"


def render_json(
    results: Sequence[CheckResult],
    facts: HostFacts,
    settings: DoctorSettings,
) -> str:
    summary = summarize(results)
    document = {
        "product": PRODUCT_NAME,
        "profile": settings.profile,
        "data_root": settings.data_root,
        "host": {
            "system": facts.system,
            "kernel_release": facts.kernel_release,
            "machine": facts.machine,
            "cpu_cores": facts.cpu_cores,
            "memory_gib": round(facts.memory_gib, 1) if facts.memory_gib is not None else None,
        },
        "checks": [result.as_dict() for result in results],
        "summary": {
            "ok": summary.ok,
            "warn": summary.warn,
            "fail": summary.fail,
            "skip": summary.skip,
            "ready": summary.ready,
            "sentence": summary.sentence(),
        },
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
