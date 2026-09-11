"""Running the container engine from the installer and the CLI, behind one interface.

Every command is an argv list with a timeout; nothing is ever joined into a shell line and no
secret is ever placed on argv (CLAUDE.md §11). `RealEngineRunner` runs `docker` or `podman`;
`FakeEngineRunner` journals every argv and replays canned answers so the install steps and
`slas user|logs` are tested without an engine (CLAUDE.md §0.3: tests run against fakes).

`Compose` is the one place that knows how `<engine> compose` is invoked: the same project name,
compose file and env file for install.sh, `slas user`, `slas logs` and the deploy test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

EXIT_NOT_FOUND = 127
EXIT_TIMEOUT = 124

COMPOSE_PROJECT = "slas"


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class EngineRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 60.0,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult: ...


@dataclass(frozen=True)
class Compose:
    """Builds argv for `<engine> compose …` so every caller uses the same project and files."""

    engine: str
    compose_file: str
    env_file: str
    project: str = COMPOSE_PROJECT

    def argv(self, *args: str) -> list[str]:
        return [
            self.engine,
            "compose",
            "--project-name",
            self.project,
            "-f",
            self.compose_file,
            "--env-file",
            self.env_file,
            *args,
        ]

    def exec_api(self, *args: str) -> list[str]:
        """Run a command inside the api container without a TTY (safe from cron and CI)."""
        return self.argv("exec", "-T", "api", *args)


class RealEngineRunner:
    """Runs the real engine. The executable is resolved to an absolute path first."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 60.0,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        if not argv:
            return RunResult(EXIT_NOT_FOUND, "", "no command given")
        executable = shutil.which(argv[0])
        if executable is None:
            return RunResult(EXIT_NOT_FOUND, "", f"{argv[0]} was not found on PATH")
        merged = dict(os.environ)
        if env:
            merged.update(env)
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, absolute executable, no shell
                [executable, *argv[1:]],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                input=input_text,
                env=merged,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunResult(EXIT_TIMEOUT, "", f"{argv[0]} did not finish within {timeout_s:.0f} s")
        except OSError as exc:
            return RunResult(EXIT_NOT_FOUND, "", str(exc))
        return RunResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass
class FakeEngineRunner:
    """Journals every argv and answers from `responses`.

    Keys are argv prefixes. The longest matching prefix wins. A list value is consumed one
    result per call so a test can script "not healthy, not healthy, healthy". An argv with no
    matching prefix returns `default`.
    """

    responses: dict[tuple[str, ...], RunResult | list[RunResult]] = field(default_factory=dict)
    default: RunResult = RunResult(0, "", "")
    calls: list[tuple[str, ...]] = field(default_factory=list)
    inputs: list[str | None] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 60.0,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        argv_t = tuple(argv)
        self.calls.append(argv_t)
        self.inputs.append(input_text)
        best: tuple[str, ...] | None = None
        for prefix in self.responses:
            if argv_t[: len(prefix)] == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            return self.default
        answer = self.responses[best]
        if isinstance(answer, list):
            if len(answer) > 1:
                return answer.pop(0)
            return answer[0]
        return answer

    def calls_with(self, *prefix: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[: len(prefix)] == prefix]
