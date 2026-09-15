"""`slas target list|show|add|arm|disarm`: the arming gate from the host command line."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from slas_cli.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, main
from slas_hal.targets import TargetRegistry

RECORD = {
    "alias": "lab-gx8-01",
    "bmc": {
        "host": "10.20.30.40",
        "user": "slas-validation",
        "password_ref": "env:LAB_GX8_01_BMC_PASSWORD",
    },
    "ssh": {
        "host": "10.20.30.41",
        "user": "slas",
        "private_key_ref": "env:LAB_GX8_01_SSH_KEY",
        "known_hosts_line": "10.20.30.41 ssh-ed25519 AAAA",
    },
}


def run(argv: list[str], data_root: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["target", "--data-root", str(data_root), *argv], stdout=out, environ={})
    return code, out.getvalue()


def test_add_list_arm_disarm(tmp_path: Path) -> None:
    code, output = run(["list"], tmp_path)
    assert (
        code == EXIT_OK
        and output == "No target is registered yet. Add one with `slas target add <file.json>`.\n"
    )

    spec = tmp_path / "gx8.json"
    spec.write_text(json.dumps(RECORD), encoding="utf-8")
    code, output = run(["add", str(spec)], tmp_path)
    assert code == EXIT_OK
    assert output == (
        "Added lab-gx8-01: BMC 10.20.30.40 as slas-validation; SSH 10.20.30.41 as slas; no PDU; "
        "power actions not armed.\n"
        "Power actions stay off until a person confirms the machine is free and runs "
        "`slas target arm lab-gx8-01`.\n"
    )
    code, output = run(["list"], tmp_path)
    assert code == EXIT_OK and output.startswith("1 target:\n  lab-gx8-01: BMC 10.20.30.40")

    code, output = run(["arm", "lab-gx8-01", "--by", "lee", "--note", "Rack 4 is clear."], tmp_path)
    assert code == EXIT_OK
    assert output.startswith("Armed lab-gx8-01: power actions may run. Recorded: lee, ")
    assert output.rstrip().endswith("(Rack 4 is clear.)")
    assert (
        TargetRegistry(tmp_path / "Validation" / "targets.json")
        .get("lab-gx8-01")
        .power_actions_enabled
    )

    code, output = run(["show", "lab-gx8-01"], tmp_path)
    assert (
        code == EXIT_OK and "armed by lee at" in output and "env:LAB_GX8_01_BMC_PASSWORD" in output
    )

    code, output = run(["disarm", "lab-gx8-01"], tmp_path)
    assert code == EXIT_OK
    assert output == "Disarmed lab-gx8-01: every power action is refused until it is armed again.\n"

    code, output = run(["arm", "lab-nope", "--by", "lee"], tmp_path)
    assert code == EXIT_PROBLEMS
    assert output.startswith("There is no target called lab-nope.\nLikely cause:")


def test_a_record_with_a_literal_secret_is_refused(tmp_path: Path) -> None:
    spec = tmp_path / "bad.json"
    bad = {**RECORD, "bmc": {**RECORD["bmc"], "password_ref": "hunter2-plain"}}  # type: ignore[dict-item]
    spec.write_text(json.dumps(bad), encoding="utf-8")
    code, output = run(["add", str(spec)], tmp_path)
    assert code == EXIT_PROBLEMS
    assert output.startswith("bad.json is not a usable target record.")
    assert "hunter2" not in output, "the literal is not echoed back"
    spec.write_text("{not json", encoding="utf-8")
    code, output = run(["add", str(spec)], tmp_path)
    assert code == EXIT_PROBLEMS and "is not JSON" in output
    with pytest.raises(SystemExit):  # `slas target` alone prints the help, like `toolchain`
        run([], tmp_path)
    assert EXIT_USAGE == 2
