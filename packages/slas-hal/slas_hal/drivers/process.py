"""The process boundary of the drivers: argv lists, an explicit environment, no shell.

`ProcessRunner` runs a command to completion (ipmitool, ssh). `StreamRunner` starts a
long-lived one and hands back its stdout line by line (`ipmitool sol activate`). Both have a
fake so no test needs a BMC, a network or the binaries.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from queue import Empty, Queue
from typing import Protocol

from slas_hal.hal import CommandResult

Handler = Callable[[Sequence[str], Mapping[str, str]], CommandResult]


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin: str | None = None,
        timeout_s: int = 600,
    ) -> CommandResult: ...


class LocalProcessRunner:
    """subprocess with a minimal environment; records argv so a test can grep it."""

    def __init__(self, *, base_path: str = "/usr/bin:/bin") -> None:
        self.base_path = base_path
        self.argv_seen: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin: str | None = None,
        timeout_s: int = 600,
    ) -> CommandResult:
        self.argv_seen.append(list(argv))
        try:
            completed = subprocess.run(  # noqa: S603 — argv list, no shell, explicit env
                list(argv),
                env={"PATH": self.base_path, "LANG": "C.UTF-8", **env},
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(exit_code=127, stderr=f"{argv[0]}: not found on this host")
        except subprocess.TimeoutExpired:
            return CommandResult(exit_code=124, stderr=f"{argv[0]} did not finish in {timeout_s} s")
        return CommandResult(
            exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
        )


def _contains(argv: Sequence[str], prefix: tuple[str, ...]) -> bool:
    """`prefix` appears as a contiguous run somewhere in argv (after the options)."""
    if not prefix:
        return True
    items = list(argv)
    return any(tuple(items[i : i + len(prefix)]) == prefix for i in range(len(items)))


def remote_argv(argv: Sequence[str]) -> list[str]:
    """For `ssh … -- cmd args`, the command run on the target; otherwise argv itself."""
    items = list(argv)
    if items and items[0] == "ssh" and "--" in items:
        return items[items.index("--") + 1 :]
    return items


class FakeProcessRunner:
    """Scripted answers. `on("ssh", "dmesg", …)` matches the remote command's prefix;
    `on("ipmitool", "chassis", "power", "status")` matches the end of the argv, after the
    connection options. A handler sees argv and env so it can consult a `FakeTarget`."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._scripts: list[tuple[str, tuple[str, ...], Handler]] = []

    def on(
        self,
        program: str,
        *prefix: str,
        result: CommandResult | None = None,
        handler: Handler | None = None,
    ) -> None:
        answer = handler or (lambda argv, env: result or CommandResult(exit_code=0))
        self._scripts.append((program, prefix, answer))

    def argv_seen(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin: str | None = None,
        timeout_s: int = 600,
    ) -> CommandResult:
        self.calls.append((list(argv), dict(env)))
        program = argv[0] if argv else ""
        remote = remote_argv(argv)
        for wanted, prefix, handler in self._scripts:
            if wanted != program:
                continue
            if program == "ssh":
                if tuple(remote[: len(prefix)]) == prefix:
                    return handler(argv, env)
            elif _contains(argv[1:], prefix):
                return handler(argv, env)
        return CommandResult(exit_code=127, stderr=f"fake runner: no script for {program} {remote}")


class StreamHandle(Protocol):
    def readline(self, timeout_s: float) -> str | None: ...

    def stop(self) -> None: ...

    def running(self) -> bool: ...


class StreamRunner(Protocol):
    def start(self, argv: Sequence[str], *, env: Mapping[str, str]) -> StreamHandle: ...


class _PopenHandle:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._lines: Queue[str] = Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.process.stdout is not None  # noqa: S101 — Popen opened with stdout=PIPE
        for line in self.process.stdout:
            self._lines.put(line.rstrip("\r\n"))

    def readline(self, timeout_s: float) -> str | None:
        try:
            return self._lines.get(timeout=timeout_s)
        except Empty:
            return None

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def running(self) -> bool:
        return self.process.poll() is None


class LocalStreamRunner:
    def __init__(self, *, base_path: str = "/usr/bin:/bin") -> None:
        self.base_path = base_path

    def start(self, argv: Sequence[str], *, env: Mapping[str, str]) -> StreamHandle:
        process = subprocess.Popen(  # noqa: S603 — argv list, no shell, explicit env
            list(argv),
            env={"PATH": self.base_path, "LANG": "C.UTF-8", **env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        return _PopenHandle(process)


class FakeStreamHandle:
    def __init__(self, source: Callable[[], list[str]]) -> None:
        self.source = source
        self.delivered = 0
        self._running = True

    def readline(self, timeout_s: float) -> str | None:
        lines = self.source()
        if self.delivered < len(lines):
            line = lines[self.delivered]
            self.delivered += 1
            return line
        return None

    def stop(self) -> None:
        self._running = False

    def running(self) -> bool:
        return self._running


class FakeStreamRunner:
    """Streams whatever `source()` returns (for example a `FakeTarget`'s console list)."""

    def __init__(self, source: Callable[[], list[str]]) -> None:
        self.source = source
        self.started: list[tuple[list[str], dict[str, str]]] = []
        self.handles: list[FakeStreamHandle] = []

    def start(self, argv: Sequence[str], *, env: Mapping[str, str]) -> StreamHandle:
        self.started.append((list(argv), dict(env)))
        handle = FakeStreamHandle(self.source)
        self.handles.append(handle)
        return handle
