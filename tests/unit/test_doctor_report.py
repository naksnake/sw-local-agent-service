"""Text and JSON rendering of the preflight, the summary sentence and the exit code."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest

from slas_cli.doctor.checks import CheckResult, DoctorSettings, describe_host, run_checks
from slas_cli.doctor.fakes import FakeHost
from slas_cli.doctor.report import (
    Summary,
    exit_code,
    render_json,
    render_text,
    summarize,
    supports_unicode,
)
from slas_schemas.errors import ThreePartMessage

SETTINGS = DoctorSettings(data_root="/AI/Agent", profile="quickstart")


def _result(status: str, check_id: str = "x") -> CheckResult:
    detail = None
    if status in ("warn", "fail"):
        detail = ThreePartMessage("Something happened.", "Because.", "Do this.")
    return CheckResult(check_id, check_id.title(), status, "Something happened.", detail)  # type: ignore[arg-type]


def test_summarize_counts_each_status() -> None:
    results = [_result("ok"), _result("ok"), _result("warn"), _result("fail"), _result("skip")]
    summary = summarize(results)
    assert (summary.ok, summary.warn, summary.fail, summary.skip) == (2, 1, 1, 1)
    assert summary.ready is False


@pytest.mark.parametrize(
    ("warn", "fail", "sentence"),
    [
        (0, 0, "Everything looks ready. You can install."),
        (1, 0, "Ready to install. 1 item is worth a look first."),
        (3, 0, "Ready to install. 3 items are worth a look first."),
        (0, 1, "1 problem must be fixed before installing."),
        (0, 2, "2 problems must be fixed before installing."),
        (1, 2, "2 problems must be fixed before installing. 1 more item is worth a look."),
        (6, 3, "3 problems must be fixed before installing. 6 more items are worth a look."),
    ],
)
def test_summary_sentence(warn: int, fail: int, sentence: str) -> None:
    assert Summary(ok=4, warn=warn, fail=fail, skip=0).sentence() == sentence


def test_exit_code_is_zero_unless_something_failed() -> None:
    assert exit_code([_result("ok"), _result("warn"), _result("skip")]) == 0
    assert exit_code([_result("ok"), _result("fail")]) == 1
    assert exit_code([]) == 0


def test_text_report_for_a_healthy_host() -> None:
    host = FakeHost.healthy()
    results = run_checks(host, SETTINGS)
    text = render_text(results, describe_host(host), SETTINGS)
    lines = text.splitlines()
    assert lines[0] == "SW Local Agent Service — preflight"
    assert lines[1] == "Checks this host only. Nothing is sent anywhere."
    assert lines[2].startswith("Host: Linux 6.8.0-45-generic on x86_64 · 64 cores")
    assert lines[3] == "Profile: quickstart · Data root: /AI/Agent"
    assert sum(1 for line in lines if line.startswith("  ✓ ")) == len(results)
    assert "Likely cause:" not in text
    assert lines[-1] == "Summary: Everything looks ready. You can install."
    assert text.endswith("\n")


def test_text_report_explains_problems_in_three_parts() -> None:
    host = FakeHost.healthy()
    del host.commands["nvidia-smi"]
    results = run_checks(host, SETTINGS)
    text = render_text(results, describe_host(host), SETTINGS)
    assert "  ✗ GPU " in text
    gpu_line = next(line for line in text.splitlines() if line.startswith("  ✗ GPU"))
    assert gpu_line.endswith("No NVIDIA GPU driver was found (nvidia-smi is missing).")
    assert "Likely cause: Inference runs on local GPUs." in text
    assert "What to do: Install the NVIDIA driver" in text
    assert "  - GPU in containers  Skipped because no GPU driver was found." in text
    assert "Summary: 1 problem must be fixed before installing." in text
    assert text.rstrip().endswith("Fix the problems above and run ./install.sh again.")


def test_text_report_aligns_summaries_in_one_column() -> None:
    host = FakeHost.healthy()
    results = run_checks(host, SETTINGS)
    text = render_text(results, describe_host(host), SETTINGS)
    check_lines = [line for line in text.splitlines() if line.startswith("  ✓ ")]
    assert len(check_lines) == len(results)
    columns = {
        line.index(result.summary) for line, result in zip(check_lines, results, strict=True)
    }
    assert len(columns) == 1
    (column,) = columns
    assert column == 4 + max(len(result.title) for result in results) + 2


def test_ascii_report_has_no_special_characters() -> None:
    host = FakeHost.healthy()
    del host.commands["docker"]
    results = run_checks(host, SETTINGS)
    text = render_text(results, describe_host(host), SETTINGS, unicode=False)
    assert text.isascii()
    assert text.splitlines()[0] == "SW Local Agent Service - preflight"
    assert "  x Container runtime  Docker was not found." in text
    assert "  + Operating system" in text


def test_json_report_round_trips() -> None:
    host = FakeHost.healthy()
    del host.commands["runsc"]
    results = run_checks(host, SETTINGS)
    document = json.loads(render_json(results, describe_host(host), SETTINGS))
    assert document["product"] == "SW Local Agent Service"
    assert document["profile"] == "quickstart"
    assert document["data_root"] == "/AI/Agent"
    assert document["host"] == {
        "system": "Linux",
        "kernel_release": "6.8.0-45-generic",
        "machine": "x86_64",
        "cpu_cores": 64,
        "memory_gib": 512.0,
    }
    assert len(document["checks"]) == len(results)
    isolation = next(check for check in document["checks"] if check["id"] == "sandbox_isolation")
    assert isolation["status"] == "warn"
    assert isolation["what_to_do"].startswith("For stronger isolation")
    assert document["summary"]["ready"] is True
    assert document["summary"]["warn"] == 1
    assert document["summary"]["sentence"] == "Ready to install. 1 item is worth a look first."


def test_json_report_with_unknown_host_facts() -> None:
    host = FakeHost(cpus=None, memory_bytes=None)
    document = json.loads(render_json([], describe_host(host), SETTINGS))
    assert document["host"]["cpu_cores"] is None
    assert document["host"]["memory_gib"] is None
    assert document["checks"] == []


@dataclass
class _Stream:
    encoding: str | None


def test_supports_unicode_depends_on_the_stream_encoding() -> None:
    assert supports_unicode(_Stream("utf-8")) is True  # type: ignore[arg-type]
    assert supports_unicode(_Stream("UTF-8")) is True  # type: ignore[arg-type]
    assert supports_unicode(_Stream("ascii")) is False  # type: ignore[arg-type]
    assert supports_unicode(_Stream("latin-1")) is False  # type: ignore[arg-type]
    assert supports_unicode(_Stream(None)) is False  # type: ignore[arg-type]
    assert supports_unicode(io.StringIO()) is False
