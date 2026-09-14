"""The sandbox runtime boundary and its fake.

The Podman driver implementing `SandboxRuntime` (rootless `podman run` / `podman exec` over
the argv `spec.podman_argv` and `spec.exec_argv` produce) arrives with its approved
dependency and the socket decision; every test runs against `FakeSandboxRuntime`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import Field

from slas_sandbox_manager.spec import WORKSPACE, SandboxSpec
from slas_schemas.common import SlasModel


class ExecResult(SlasModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxHandle(SlasModel):
    id: str = Field(min_length=1)
    spec: SandboxSpec

    @property
    def name(self) -> str:
        return self.spec.name


class SandboxRuntime(Protocol):
    def create(self, spec: SandboxSpec) -> SandboxHandle: ...

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult: ...

    def destroy(self, handle: SandboxHandle) -> None: ...

    def alive(self, handle: SandboxHandle) -> bool: ...


ExecHandler = Callable[[Sequence[str], str], ExecResult | None]


class FakeSandboxRuntime:
    """Sandboxes as records; commands answered by scripted results or a handler."""

    def __init__(self) -> None:
        self._alive: dict[str, SandboxHandle] = {}
        self.created: list[SandboxSpec] = []
        self.destroyed: list[str] = []
        self.execs: list[tuple[str, tuple[str, ...], str]] = []
        self._scripts: dict[tuple[str, ...], list[ExecResult]] = {}
        self._handler: ExecHandler | None = None
        self._counter = 0

    def script(self, argv: Sequence[str], *results: ExecResult) -> None:
        """Answer `argv` with the given results in order; the last one repeats."""
        self._scripts.setdefault(tuple(argv), []).extend(results)

    def handle_with(self, handler: ExecHandler) -> None:
        self._handler = handler

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        if spec.name in self._alive:
            raise RuntimeError(f"a sandbox named {spec.name} is already running")
        self._counter += 1
        handle = SandboxHandle(id=f"sbx{self._counter:03d}", spec=spec)
        self._alive[spec.name] = handle
        self.created.append(spec)
        return handle

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        if handle.name not in self._alive:
            raise RuntimeError(f"sandbox {handle.name} is not running")
        self.execs.append((handle.name, tuple(argv), cwd))
        if self._handler is not None:
            answer = self._handler(argv, cwd)
            if answer is not None:
                return answer
        queue = self._scripts.get(tuple(argv))
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return ExecResult(exit_code=0, stdout="")

    def destroy(self, handle: SandboxHandle) -> None:
        self._alive.pop(handle.name, None)
        self.destroyed.append(handle.name)

    def alive(self, handle: SandboxHandle) -> bool:
        return handle.name in self._alive
