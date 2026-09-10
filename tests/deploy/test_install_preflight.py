"""`./install.sh` runs the preflight and prints a plain-language report (P0 done-when).

The host is faked with PATH shims, so the test needs neither a container engine nor a GPU
and runs under the egress-DROP job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


def write_shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def shims(tmp_path: Path) -> Path:
    """A PATH with a healthy fake host: podman, compose, nvidia-smi, runsc, and this Python."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_shim(
        bin_dir,
        "podman",
        'case "$1" in\n'
        '  --version) echo "podman version 5.2.3" ;;\n'
        '  compose) echo "podman-compose version 1.2.0" ;;\n'
        "esac",
    )
    write_shim(bin_dir, "nvidia-smi", 'echo "NVIDIA L40S, 46068, 550.90.07"')
    write_shim(bin_dir, "runsc", "exit 0")
    os.symlink(sys.executable, bin_dir / "python3")
    return bin_dir


def run_install(bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(bin_dir.parent),
        "LC_ALL": "C.UTF-8",
    }
    return subprocess.run(
        ["/bin/sh", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(bin_dir.parent),
        timeout=60,
        check=False,
    )


def test_install_sh_is_posix_sh_and_executable() -> None:
    first = INSTALL_SH.read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh"
    assert os.access(INSTALL_SH, os.X_OK)


def test_preflight_passes_on_a_healthy_fake_host(shims: Path, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    result = run_install(shims, "--data-root", str(data_root), "--edge-port", "0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "podman version 5.2.3." in result.stdout
    assert "1 GPU (NVIDIA L40S)" in result.stdout
    assert "Preflight passed" in result.stdout
    assert "nothing was changed on this host" in result.stdout
    assert not data_root.exists(), "preflight must not create the data root"


def test_preflight_is_idempotent(shims: Path, tmp_path: Path) -> None:
    first = run_install(shims, "--data-root", str(tmp_path / "data"), "--edge-port", "0")
    second = run_install(shims, "--data-root", str(tmp_path / "data"), "--edge-port", "0")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_broken_engine_is_reported_in_three_parts(shims: Path, tmp_path: Path) -> None:
    write_shim(shims, "podman", "exit 125")
    result = run_install(shims, "--data-root", str(tmp_path / "data"), "--edge-port", "0")
    assert result.returncode == 2
    assert "Blocked" in result.stdout
    assert "What happened:" in result.stdout
    assert "Likely cause:" in result.stdout
    assert "What to do:" in result.stdout
    assert "nothing was changed on this host" in result.stdout


def test_missing_python_is_reported_in_three_parts(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # coreutils stay reachable through /usr/bin and /bin, but python3 is hidden by a shim
    # that pretends to be too old, and python3.12 is absent.
    write_shim(bin_dir, "python3", "exit 1")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"}
    if shutil.which("python3.12", path=env["PATH"]) is not None:
        write_shim(bin_dir, "python3.12", "exit 1")
    result = subprocess.run(
        ["/bin/sh", str(INSTALL_SH), "--data-root", str(tmp_path / "data")],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "What happened:" in result.stderr
    assert "Python 3.12" in result.stderr


def test_help_and_unknown_option(shims: Path) -> None:
    ok = run_install(shims, "--help")
    assert ok.returncode == 0 and "preflight" in ok.stdout.lower()
    bad = run_install(shims, "--frobnicate")
    assert bad.returncode == 2 and "--frobnicate" in bad.stderr


def test_json_output_for_ci(shims: Path, tmp_path: Path) -> None:
    import json

    result = run_install(shims, "--data-root", str(tmp_path / "data"), "--edge-port", "0", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)  # --json prints the report only, nothing after it
    assert payload["exit_code"] == 0
    assert {c["id"] for c in payload["checks"]} >= {"container_engine", "gpu", "data_root"}
