"""The Terminal tab's session: lines run inside the sandbox; transcript redacted; push explained."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from slas_kernel.clock import FakeClock
from slas_sandbox_manager.manager import PUSH_EXPLANATION, SandboxManager
from slas_sandbox_manager.runtime import ExecResult, FakeSandboxRuntime
from slas_sandbox_manager.terminal import TerminalMessage, TerminalSession


def echo(argv: Sequence[str], cwd: str) -> ExecResult | None:
    line = argv[2] if len(argv) == 3 else ""
    if line.startswith("git push"):
        return ExecResult(
            exit_code=128,
            stderr=(
                "fatal: unable to access 'http://gitlab.internal/x.git/': "
                "Could not resolve host: gitlab.internal"
            ),
        )
    return ExecResult(exit_code=0, stdout=f"ran: {line}\n")


def test_terminal_runs_lines_in_the_sandbox_and_explains_push(tmp_path: Path) -> None:
    runtime = FakeSandboxRuntime()
    runtime.handle_with(echo)
    clock = FakeClock(datetime(2026, 9, 14, 9, tzinfo=UTC), step=timedelta(0))
    manager = SandboxManager(runtime=runtime, data_root=tmp_path, clock=clock, runsc_available=True)
    session = manager.open(
        "pat",
        "bmc",
        image="registry.internal/slas/sandbox-python:3.12.6",
        language="python",
        display_name="Pat",
    )
    terminal = TerminalSession(
        manager,
        session.id,
        clock=clock,
        record_path=tmp_path / "Tickets" / "T-coding-0001" / "terminal.jsonl",
    )

    assert terminal.run("   ") is None
    first = terminal.run("git log --oneline")
    assert (
        first is not None
        and first.command == "git log --oneline"
        and first.output == "ran: git log --oneline"
    )
    assert (
        runtime.execs[-1][1] == ("bash", "-lc", "git log --oneline")
        and runtime.execs[-1][2] == "/workspace"
    )

    push = terminal.run("git push origin main")
    assert push is not None and push.exit_code == 128
    assert push.output.endswith("\n" + PUSH_EXPLANATION) and "Could not resolve host" in push.output

    pasted = terminal.run("export GITLAB_TOKEN=glpat-abcdefghijklmnopqrst && echo ok")
    assert (
        pasted is not None
        and "glpat-" not in pasted.command
        and "[redacted:gitlab_pat]" in pasted.command
    )
    assert "glpat-" not in pasted.output

    frames = terminal.handle(TerminalMessage(type="input", text="ls"))
    assert [f.type for f in frames] == ["output", "exit"] and frames[0].text == "ran: ls"
    assert terminal.handle(TerminalMessage(type="output", text="x"))[0].type == "notice"
    assert terminal.handle(TerminalMessage(type="input", text="")) == []

    transcript = terminal.transcript()
    assert transcript.startswith(
        "$ git log --oneline\nran: git log --oneline\n$ git push origin main"
    )
    recorded = [
        json.loads(line)
        for line in (tmp_path / "Tickets" / "T-coding-0001" / "terminal.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [r["n"] for r in recorded] == [1, 2, 3, 4] and "glpat-" not in json.dumps(recorded)
    assert terminal.run("x" * 5000) is not None and len(runtime.execs[-1][1][2]) == 4000
