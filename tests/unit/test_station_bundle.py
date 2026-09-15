"""The station bundle scripts (P10) parse, carry no secret, and say the right things."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "deploy" / "station-runner"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed to parse the scripts")
@pytest.mark.parametrize("script", ["build-bundle.sh", "install.sh"])
def test_shell_scripts_parse_and_are_executable(script: str) -> None:
    path = BUNDLE / script
    subprocess.run(["bash", "-n", str(path)], check=True, timeout=30)
    assert path.stat().st_mode & 0o111, f"{script} must be executable"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


def test_the_bundle_installs_offline_and_ships_only_the_public_ca() -> None:
    build = (BUNDLE / "build-bundle.sh").read_text(encoding="utf-8")
    assert (
        "--only-binary=:all:" in build and "win_amd64" in build and "manylinux2014_x86_64" in build
    )
    assert "Factory/ca/ca.pem" in build and "slas-ca.pem" in build
    assert "ca.key" not in build.replace("ca.pem", ""), "the CA key never leaves the platform"
    for installer in ("install.sh", "install.ps1"):
        text = (BUNDLE / installer).read_text(encoding="utf-8")
        assert "--no-index" in text and "--find-links" in text, installer
        assert "slas-ca.pem" in text and "enrol" in text and "doctor" in text, installer
        assert "XXXX-XXXX-XXXX" in text, installer
        assert not re.search(r"(?i)(password|token)\s*=", text), installer
    assert "PyAutoGUI" in (BUNDLE / "install.ps1").read_text(encoding="utf-8")
    assert "Register-ScheduledTask" in (BUNDLE / "install.ps1").read_text(encoding="utf-8")


def test_the_systemd_unit_runs_in_the_graphical_session_without_privileges() -> None:
    unit = (BUNDLE / "slas-station-runner.service").read_text(encoding="utf-8")
    assert "WantedBy=graphical-session.target" in unit
    assert "ExecStart=@STATE@/venv/bin/slas-station-runner --state-dir @STATE@ serve" in unit
    assert "ExecStartPre=@STATE@/venv/bin/slas-station-runner --state-dir @STATE@ doctor" in unit
    assert "NoNewPrivileges=yes" in unit and "UMask=0077" in unit
    assert "User=root" not in unit
    assert (BUNDLE / "README.md").read_text(encoding="utf-8").count("|") > 10
