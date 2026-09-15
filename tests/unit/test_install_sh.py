"""`./install.sh` runs the preflight from the source tree and prints a report.

These tests run the real script against the machine running the tests. They accept either
"ready" or "problems found" because a CI runner has no GPU; what they assert is the
contract: a plain-language report, the right exit codes, and no change to the host.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


def run_install(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SLAS_") and key != "PYTHONPATH"
    }
    clean_env.update(env or {})
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )


def test_install_sh_is_executable() -> None:
    assert os.access(INSTALL_SH, os.X_OK), "install.sh must keep its executable bit"


def test_help_describes_the_options() -> None:
    result = run_install("--help")
    assert result.returncode == 0
    assert result.stdout.startswith("Usage: ./install.sh")
    assert "--profile" in result.stdout
    assert "--data-root" in result.stdout
    assert "Exit codes: 0 ready" in result.stdout


def test_preflight_prints_a_report_and_changes_nothing(tmp_path: Path) -> None:
    data_root = tmp_path / "slas-data"
    result = run_install("--data-root", str(data_root))
    assert result.returncode in (0, 1), result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("SW Local Agent Service")
    assert lines[0].endswith("preflight")
    assert f"Data root: {data_root}" in result.stdout
    assert "Summary:" in result.stdout
    assert "Nothing was changed on this host." in result.stdout
    if result.returncode == 0:
        assert "Preflight passed." in result.stdout
    else:
        assert "Preflight found problems." in result.stdout
        assert "then run ./install.sh again." in result.stdout
    assert not data_root.exists(), "the preflight must not create the data root"


def test_json_output_is_machine_readable(tmp_path: Path) -> None:
    result = run_install("--json", "--data-root", str(tmp_path / "d"))
    assert result.returncode in (0, 1), result.stderr
    document = json.loads(result.stdout)
    assert document["product"] == "SW Local Agent Service"
    assert document["data_root"] == str(tmp_path / "d")
    assert {check["id"] for check in document["checks"]} >= {"gpu", "data_root", "web_port"}
    assert document["summary"]["ready"] is (result.returncode == 0)


def test_environment_variables_are_honoured(tmp_path: Path) -> None:
    env = {"SLAS_DATA_ROOT": str(tmp_path / "from-env"), "SLAS_PROFILE": "prod"}
    result = run_install("--json", env=env)
    document = json.loads(result.stdout)
    assert document["data_root"] == str(tmp_path / "from-env")
    assert document["profile"] == "prod"


def test_preflight_only_flag_is_accepted(tmp_path: Path) -> None:
    result = run_install("--preflight-only", "--json", "--data-root=" + str(tmp_path))
    assert result.returncode in (0, 1)
    assert json.loads(result.stdout)["data_root"] == str(tmp_path)


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (("--profile", "nope"), 'The profile "nope" is not known.'),
        (("--profile=nope",), "What to do: use --profile quickstart"),
        (("--bogus",), "Unknown option: --bogus"),
        (("--data-root", ""), "The data root is empty."),
    ],
)
def test_wrong_usage_exits_with_two_and_explains(args: tuple[str, ...], fragment: str) -> None:
    result = run_install(*args)
    assert result.returncode == 2
    assert fragment in result.stderr
    assert result.stdout == ""
