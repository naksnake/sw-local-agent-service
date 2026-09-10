"""`slas doctor` against the fake host: sentences, three-part errors, exit codes."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from slas_cli import cli
from slas_cli.doctor import GIB, Thresholds, run_doctor
from slas_cli.fakes import FakeHostProbe
from slas_cli.probe import CommandResult
from slas_cli.report import EXIT_BLOCKED, EXIT_OK, STATUS_WORDS, Report, render_json, render_text


def by_id(report: Report, check_id: str) -> str:
    matching = [c for c in report.checks if c.id == check_id]
    assert len(matching) == 1, f"expected exactly one check {check_id}"
    return matching[0].status


def test_healthy_host_passes_with_no_warnings() -> None:
    report = run_doctor(FakeHostProbe(), data_root="/AI/Agent")
    assert report.exit_code == EXIT_OK
    assert not report.blocked and not report.warnings
    assert report.summary() == "Preflight passed. This host meets every requirement."
    text = render_text(report)
    assert (
        "2 GPUs (NVIDIA H100 80GB HBM3), 159 GiB of GPU memory in total, driver 550.90.07." in text
    )
    assert "podman version 5.2.3." in text
    assert "/AI/Agent exists with 2000 GiB free." in text


def test_missing_engine_is_a_three_part_blocker() -> None:
    probe = FakeHostProbe(executables={"nvidia-smi": "/usr/bin/nvidia-smi"})
    report = run_doctor(probe, data_root="/AI/Agent")
    assert report.exit_code == EXIT_BLOCKED
    assert by_id(report, "container_engine") == "blocked"
    assert by_id(report, "compose") == "blocked"
    engine = next(c for c in report.checks if c.id == "container_engine")
    assert engine.error is not None
    assert engine.error.what_happened and engine.error.likely_cause and engine.error.what_to_do
    text = render_text(report)
    assert "What happened:" in text and "Likely cause:" in text and "What to do:" in text
    assert "blocking checks" in report.summary()


def test_engine_present_but_not_answering_is_blocked() -> None:
    probe = FakeHostProbe()
    probe.commands[("podman", "--version")] = CommandResult(125, "", "cannot connect")
    report = run_doctor(probe, data_root="/AI/Agent")
    assert by_id(report, "container_engine") == "blocked"
    assert "did not answer" in next(c for c in report.checks if c.id == "container_engine").sentence


def test_docker_is_accepted_when_podman_is_absent() -> None:
    probe = FakeHostProbe(
        executables={"docker": "/usr/bin/docker", "runsc": "/usr/local/bin/runsc"},
        commands={
            ("docker", "--version"): CommandResult(0, "Docker version 27.3.1, build ce12230\n", ""),
            ("docker", "compose", "version"): CommandResult(
                0, "Docker Compose version v2.29.7\n", ""
            ),
        },
    )
    report = run_doctor(probe, data_root="/AI/Agent")
    assert by_id(report, "container_engine") == "ok"
    assert by_id(report, "compose") == "ok"
    assert by_id(report, "gpu") == "warning"  # no nvidia-smi in this fake
    assert report.exit_code == EXIT_OK


def test_no_gpu_is_a_warning_that_says_what_will_happen() -> None:
    probe = FakeHostProbe(
        executables={"podman": "/usr/bin/podman", "runsc": "/usr/local/bin/runsc"}
    )
    report = run_doctor(probe, data_root="/AI/Agent")
    gpu = next(c for c in report.checks if c.id == "gpu")
    assert gpu.status == "warning"
    assert "Inference will not be available" in gpu.sentence
    assert report.exit_code == EXIT_OK
    assert report.summary().startswith("Preflight passed with 1 warning.")


def test_missing_gvisor_names_the_fallback() -> None:
    probe = FakeHostProbe(
        executables={"podman": "/usr/bin/podman", "nvidia-smi": "/usr/bin/nvidia-smi"}
    )
    report = run_doctor(probe, data_root="/AI/Agent")
    gvisor = next(c for c in report.checks if c.id == "gvisor")
    assert gvisor.status == "warning"
    assert "hardened runc" in gvisor.sentence


def test_low_disk_and_small_host_are_warnings_with_thresholds_stated() -> None:
    probe = FakeHostProbe(cpus=4, memory_bytes=16 * GIB, free_bytes=100 * GIB)
    report = run_doctor(probe, data_root="/AI/Agent")
    assert by_id(report, "cpu_memory") == "warning"
    assert by_id(report, "data_root") == "warning"
    text = render_text(report)
    assert "8 CPUs are recommended" in text and "32 GiB of memory is recommended" in text
    assert "500 GiB is recommended" in text
    assert report.exit_code == EXIT_OK


def test_thresholds_are_configurable() -> None:
    probe = FakeHostProbe(cpus=4, memory_bytes=16 * GIB, free_bytes=100 * GIB)
    small = Thresholds(min_cpus=2, min_memory_gib=8, min_disk_gib=50)
    report = run_doctor(probe, data_root="/AI/Agent", thresholds=small)
    assert not report.warnings


def test_unwritable_data_root_is_blocked() -> None:
    report = run_doctor(FakeHostProbe(writable=False), data_root="/AI/Agent")
    assert by_id(report, "data_root") == "blocked"
    assert report.exit_code == EXIT_BLOCKED


def test_new_data_root_says_it_will_be_created() -> None:
    report = run_doctor(FakeHostProbe(), data_root="/srv/slas")
    assert "will be created" in next(c for c in report.checks if c.id == "data_root").sentence


def test_busy_web_port_is_blocked() -> None:
    report = run_doctor(FakeHostProbe(busy_ports={443}), data_root="/AI/Agent")
    port = next(c for c in report.checks if c.id == "edge_port")
    assert port.status == "blocked"
    assert port.error is not None and "SLAS_EDGE_PORT" in port.error.what_to_do


def test_json_output_is_complete_and_parseable() -> None:
    report = run_doctor(FakeHostProbe(busy_ports={443}), data_root="/AI/Agent")
    payload = json.loads(render_json(report))
    assert payload["exit_code"] == EXIT_BLOCKED
    assert payload["data_root"] == "/AI/Agent"
    assert {c["id"] for c in payload["checks"]} >= {"container_engine", "gpu", "edge_port"}
    blocked = [c for c in payload["checks"] if c["status"] == "blocked"]
    assert blocked and set(blocked[0]["error"]) == {"what_happened", "likely_cause", "what_to_do"}


def test_text_report_uses_words_not_codes() -> None:
    """CLAUDE.md §9: sentences, not enums, as primary content."""
    probe = FakeHostProbe(busy_ports={443})
    del probe.executables["runsc"]  # one blocked check (port) and one warning (no gVisor)
    report = run_doctor(probe, data_root="/AI/Agent")
    text = render_text(report)
    for word in STATUS_WORDS.values():
        assert word in text
    assert "'ok'" not in text and '"blocked"' not in text
    for line in text.splitlines():
        assert line == "" or ": " in line or line.endswith("."), f"not a sentence: {line!r}"


def test_report_is_immutable() -> None:
    report = run_doctor(FakeHostProbe(), data_root="/AI/Agent")
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
        report.checks[0].status = "blocked"  # type: ignore[misc]
    assert replace(report.checks[0], status="warning").status == "warning"


def test_cli_later_commands_say_which_phase(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["model"]) == 2
    err = capsys.readouterr().err
    assert "not available yet" in err and "phase P3" in err


def test_cli_without_a_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "doctor" in capsys.readouterr().out
