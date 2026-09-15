"""RealHost against the machine running the tests: only things every Linux box has.

No GPU, no display, no container runtime is needed; every assertion holds on a bare CI
runner. The point is that the real probes answer in the shape the checks expect.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from slas_cli.doctor.host import CommandResult, RealHost

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the platform targets Linux")

host = RealHost()


def test_identity_probes_answer_with_strings() -> None:
    assert host.system() == "Linux"
    assert host.kernel_release()
    assert host.machine()


def test_resource_probes_answer_with_positive_numbers_or_none() -> None:
    cores = host.cpu_count()
    assert cores is None or cores > 0
    memory = host.memory_total_bytes()
    assert memory is None or memory > 0
    free = host.disk_free_bytes("/")
    assert free is not None and free > 0
    assert host.disk_free_bytes("/definitely/not/here") is None


def test_which_finds_installed_commands_only() -> None:
    assert host.which("sh") is not None
    assert host.which("slas-definitely-not-installed") is None


def test_run_captures_output_without_a_shell(tmp_path: Path) -> None:
    result = host.run(["sh", "-c", "echo out; echo err >&2"])
    assert result == CommandResult(0, "out\n", "err\n")
    assert result.ok
    # Shell metacharacters are passed as plain arguments, never interpreted.
    literal = host.run(["echo", "$HOME;", "&&", "echo", "x"])
    assert literal.stdout == "$HOME; && echo x\n"


def test_run_reports_missing_command_empty_argv_and_timeout() -> None:
    assert host.run(["slas-definitely-not-installed", "--version"]) == CommandResult(None, "")
    assert host.run([]) == CommandResult(None, "")
    slow = host.run(["sleep", "5"], timeout_s=0.1)
    assert slow.returncode is None
    assert not slow.ok


def test_run_reports_non_zero_exit_codes() -> None:
    assert host.run(["sh", "-c", "exit 3"]).returncode == 3


def test_filesystem_probes(tmp_path: Path) -> None:
    assert host.path_exists("/")
    assert host.is_dir("/")
    assert host.is_writable(str(tmp_path))
    file = tmp_path / "note.txt"
    file.write_text("hello\n", encoding="utf-8")
    assert host.path_exists(str(file))
    assert not host.is_dir(str(file))
    assert host.read_text(str(file)) == "hello\n"
    assert host.read_text(str(tmp_path / "missing")) is None
    assert not host.path_exists(str(tmp_path / "missing"))


def test_port_probe_sees_a_listener_and_a_free_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert host.port_in_use(port) is True
    assert host.port_in_use(port) is False


def test_env_probe_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLAS_TEST_PROBE", "yes")
    assert host.env("SLAS_TEST_PROBE") == "yes"
    monkeypatch.delenv("SLAS_TEST_PROBE")
    assert host.env("SLAS_TEST_PROBE") is None
