"""The process boundary of the broker: argv only, an explicit environment, inherited fds.

`pass_fds` is how a token reaches the GIT_ASKPASS helper: the read end of a pipe the broker
filled in memory, never argv, never the environment, never a file.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from slas_git.workspace import CommandResult


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
        pass_fds: Sequence[int] = (),
        timeout_s: int = 600,
    ) -> CommandResult: ...


class LocalProcessRunner:
    """subprocess with a minimal environment; records argv so a test can grep it."""

    def __init__(self, *, base_path: str = "/usr/bin:/bin") -> None:
        self.base_path = base_path
        self.argv_seen: list[list[str]] = []
        self.env_seen: list[dict[str, str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
        pass_fds: Sequence[int] = (),
        timeout_s: int = 600,
    ) -> CommandResult:
        self.argv_seen.append(list(argv))
        self.env_seen.append(dict(env))
        completed = subprocess.run(  # noqa: S603 — argv list, no shell, explicit env
            list(argv),
            cwd=cwd,
            env={"PATH": self.base_path, "LANG": "C.UTF-8", **env},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            pass_fds=tuple(pass_fds),
            check=False,
        )
        return CommandResult(
            exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
        )


class FakeProcessRunner:
    """Scripted results keyed by the argv suffix after `git` and its `-c` flags."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, dict[str, str], tuple[int, ...]]] = []
        self.argv_seen: list[list[str]] = []
        self.env_seen: list[dict[str, str]] = []
        self._scripts: list[tuple[tuple[str, ...], Callable[[], CommandResult]]] = []

    def script(self, *prefix: str, result: CommandResult) -> None:
        self._scripts.append((prefix, lambda: result))

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdin: str | None = None,
        pass_fds: Sequence[int] = (),
        timeout_s: int = 600,
    ) -> CommandResult:
        self.calls.append((list(argv), cwd, dict(env), tuple(pass_fds)))
        self.argv_seen.append(list(argv))
        self.env_seen.append(dict(env))
        bare = list(argv)
        if bare and bare[0] == "git":
            bare = bare[1:]
        while len(bare) >= 2 and bare[0] == "-c":
            bare = bare[2:]
        for prefix, make in self._scripts:
            if tuple(bare[: len(prefix)]) == prefix:
                return make()
        return CommandResult(exit_code=0)
