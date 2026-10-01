"""install.sh runs `slas doctor` from the source tree on a bare host: no third-party imports.

The path list comes from install.sh itself, so a module the CLI starts importing must be
added to the installer's PYTHONPATH — a hard-coded copy here once let the two drift apart
and `./install.sh` failed on a fresh host with ModuleNotFoundError.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"


def install_sh_pythonpath() -> list[Path]:
    """The directories install.sh puts on PYTHONPATH when running from the source tree."""
    match = re.search(
        r'^export PYTHONPATH="(.+?)\$\{PYTHONPATH:\+', INSTALL_SH.read_text(), re.MULTILINE
    )
    assert match, "install.sh must export PYTHONPATH for the source-tree preflight"
    entries = match.group(1).rstrip(":").split(":")
    return [Path(entry.replace("$SCRIPT_DIR", str(REPO_ROOT))) for entry in entries]


def test_install_sh_pythonpath_directories_exist() -> None:
    for directory in install_sh_pythonpath():
        assert directory.is_dir(), (
            f"install.sh puts {directory} on PYTHONPATH but it does not exist"
        )


def test_slas_doctor_imports_with_site_packages_disabled(tmp_path: Path) -> None:
    pythonpath = os.pathsep.join(str(directory) for directory in install_sh_pythonpath())
    result = subprocess.run(
        [
            sys.executable,
            "-S",  # no site-packages: pydantic and friends are invisible
            "-c",
            "import sys\n"
            "import slas_cli.cli, slas_cli.doctor, slas_schemas, slas_schemas.envfile\n"
            "import slas_sandbox_manager.toolchains\n"
            "from slas_kernel.branding import PRODUCT_NAME\n"
            # install.sh calls `slas doctor`, which builds the whole parser, sub-commands included.
            "slas_cli.cli.build_parser({})\n"
            "assert 'pydantic' not in sys.modules, 'the host CLI must not import pydantic'\n"
            "print(PRODUCT_NAME)",
        ],
        env={"PYTHONPATH": pythonpath, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SW Local Agent Service"


def test_fetch_models_runs_with_site_packages_disabled(tmp_path: Path) -> None:
    """install.sh runs `scripts/fetch_models.py verify` on the bare host as well."""
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(REPO_ROOT / "scripts" / "fetch_models.py"),
            "verify",
            "--dest",
            str(tmp_path),
        ],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "has a SHA256SUMS file" in result.stdout
