"""install.sh runs `slas doctor` from the source tree on a bare host: no third-party imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_CLI_PACKAGES = ("slas-cli", "slas-kernel", "slas-schemas")


def test_slas_doctor_imports_with_site_packages_disabled(tmp_path: Path) -> None:
    pythonpath = os.pathsep.join(str(REPO_ROOT / "packages" / name) for name in HOST_CLI_PACKAGES)
    result = subprocess.run(
        [
            sys.executable,
            "-S",  # no site-packages: pydantic and friends are invisible
            "-c",
            "import sys\n"
            "import slas_cli.cli, slas_cli.doctor, slas_schemas, slas_schemas.envfile\n"
            "from slas_kernel.branding import PRODUCT_NAME\n"
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
