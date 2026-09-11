"""The `slas` entry point: argument handling, doctor wiring, not-yet commands."""

from __future__ import annotations

import io
import json

import pytest

from slas_cli import __version__
from slas_cli.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, NOT_YET, main
from slas_cli.doctor.fakes import FakeHost


def run(argv: list[str], host: FakeHost | None = None, **environ: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, host=host or FakeHost.healthy(), stdout=out, environ=environ)
    return code, out.getvalue()


def test_doctor_json_on_a_healthy_host() -> None:
    code, output = run(["doctor", "--json", "--data-root", "/AI/Agent"])
    assert code == EXIT_OK
    document = json.loads(output)
    assert document["summary"]["ready"] is True
    assert document["data_root"] == "/AI/Agent"
    assert document["profile"] == "quickstart"


def test_doctor_reports_problems_with_exit_code_one() -> None:
    host = FakeHost.healthy()
    del host.commands["nvidia-smi"]
    code, output = run(["doctor", "--data-root", "/AI/Agent"], host=host)
    assert code == EXIT_PROBLEMS
    assert "No NVIDIA GPU driver was found" in output
    assert "Summary: 1 problem must be fixed before installing." in output


def test_doctor_falls_back_to_ascii_when_the_stream_cannot_encode() -> None:
    # io.StringIO has no encoding, so the report must stay ASCII without being asked.
    code, output = run(["doctor", "--data-root", "/AI/Agent"])
    assert code == EXIT_OK
    assert output.isascii()
    assert output.splitlines()[0] == "SW Local Agent Service - preflight"


def test_doctor_ascii_flag_is_accepted() -> None:
    code, output = run(["doctor", "--ascii", "--data-root", "/AI/Agent"])
    assert code == EXIT_OK
    assert output.isascii()


def test_doctor_reads_defaults_from_the_environment() -> None:
    host = FakeHost.healthy(data_root="/srv/slas")
    del host.commands["runsc"]  # prod requires gVisor, so this must now fail
    code, output = run(
        ["doctor", "--json"], host=host, SLAS_DATA_ROOT="/srv/slas", SLAS_PROFILE="prod"
    )
    document = json.loads(output)
    assert document["data_root"] == "/srv/slas"
    assert document["profile"] == "prod"
    assert code == EXIT_PROBLEMS


def test_doctor_default_data_root_when_nothing_is_set() -> None:
    _, output = run(["doctor", "--json"])
    assert json.loads(output)["data_root"] == "/AI/Agent"


def test_no_command_prints_help_and_exits_with_usage_code() -> None:
    code, output = run([])
    assert code == EXIT_USAGE
    assert output.startswith("usage: slas")
    assert "doctor" in output


@pytest.mark.parametrize("command", sorted(NOT_YET))
def test_not_yet_commands_say_which_phase_brings_them(command: str) -> None:
    code, output = run([command, "anything", "--goes"])
    assert code == EXIT_USAGE
    assert output == (
        f"`slas {command}` is not available yet. It arrives in {NOT_YET[command]} of "
        "docs/DEVELOPMENT_PLAN.md.\n"
    )


def test_not_yet_covers_every_command_from_claude_md_section_3() -> None:
    assert set(NOT_YET) == {
        "status",
        "logs",
        "user",
        "model",
        "toolchain",
        "skill",
        "backup",
        "upgrade",
    }


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"], host=FakeHost.healthy())
    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"slas {__version__}"


def test_unknown_profile_is_rejected_by_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["doctor", "--profile", "nope"], host=FakeHost.healthy())
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
