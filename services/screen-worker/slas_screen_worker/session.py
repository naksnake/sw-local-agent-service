"""Display sessions: Xvfb + x11vnc + noVNC per session, argv only (CLAUDE.md §5.2, ADR-0002).

The worker owns the displays; the operator watches through noVNC and can take over. No
host X11 socket or /dev/input is ever involved (INV-4). Process control goes through a
`ProcessRunner` so the manager is tested against a fake.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import Field

from slas_observability import metrics
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

FIRST_DISPLAY: Final = 10
VNC_BASE_PORT: Final = 5900
NOVNC_BASE_PORT: Final = 6080
NOVNC_WEB_ROOT: Final = "/usr/share/novnc"


class DisplaySession(SlasModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    display: int = Field(ge=FIRST_DISPLAY)
    width: int = Field(default=1920, ge=640)
    height: int = Field(default=1080, ge=480)
    depth: int = Field(default=24)

    @property
    def display_name(self) -> str:
        return f":{self.display}"

    @property
    def vnc_port(self) -> int:
        return VNC_BASE_PORT + self.display

    @property
    def novnc_port(self) -> int:
        return NOVNC_BASE_PORT + self.display

    def xvfb_argv(self) -> list[str]:
        return [
            "Xvfb",
            self.display_name,
            "-screen",
            "0",
            f"{self.width}x{self.height}x{self.depth}",
            "-nolisten",
            "tcp",
            "-noreset",
        ]

    def x11vnc_argv(self) -> list[str]:
        # -localhost: only noVNC inside the container connects; the operator uses the web view.
        return [
            "x11vnc",
            "-display",
            self.display_name,
            "-rfbport",
            str(self.vnc_port),
            "-localhost",
            "-forever",
            "-shared",
            "-nopw",
            "-noxdamage",
            "-quiet",
        ]

    def novnc_argv(self) -> list[str]:
        return [
            "websockify",
            "--web",
            NOVNC_WEB_ROOT,
            str(self.novnc_port),
            f"localhost:{self.vnc_port}",
        ]

    def sentence(self) -> str:
        return (
            f"Session {self.session_id} has display {self.display_name} "
            f"({self.width}×{self.height}); watch it on port {self.novnc_port}."
        )


class ProcessHandle(SlasModel):
    pid: int = Field(ge=1)
    argv: list[str]


class ProcessRunner(Protocol):
    def start(self, argv: Sequence[str], *, env: dict[str, str]) -> ProcessHandle: ...

    def stop(self, handle: ProcessHandle) -> None: ...

    def alive(self, handle: ProcessHandle) -> bool: ...


@dataclass
class FakeProcessRunner:
    started: list[ProcessHandle] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)
    dead: set[int] = field(default_factory=set)
    _next_pid: int = 100

    def start(self, argv: Sequence[str], *, env: dict[str, str]) -> ProcessHandle:
        self._next_pid += 1
        handle = ProcessHandle(pid=self._next_pid, argv=list(argv))
        self.started.append(handle)
        return handle

    def stop(self, handle: ProcessHandle) -> None:
        self.stopped.append(handle.pid)
        self.dead.add(handle.pid)

    def alive(self, handle: ProcessHandle) -> bool:
        return handle.pid not in self.dead


class SessionError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


@dataclass
class _Live:
    session: DisplaySession
    processes: list[ProcessHandle]


class SessionManager:
    def __init__(self, runner: ProcessRunner, *, max_displays: int = 8) -> None:
        self.runner = runner
        self.max_displays = max_displays
        self._live: dict[str, _Live] = {}

    def sessions(self) -> list[DisplaySession]:
        return [live.session for live in self._live.values()]

    def _free_display(self) -> int:
        used = {live.session.display for live in self._live.values()}
        for candidate in range(FIRST_DISPLAY, FIRST_DISPLAY + self.max_displays):
            if candidate not in used:
                return candidate
        raise SessionError(
            ThreePartMessage(
                f"All {self.max_displays} virtual displays are in use.",
                "Each session gets its own display and the worker is at its limit.",
                "Wait for a session to finish, or raise DISPLAYS_PER_WORKER and restart the "
                "screen worker.",
            )
        )

    def open(self, session_id: str, *, width: int = 1920, height: int = 1080) -> DisplaySession:
        if session_id in self._live:
            return self._live[session_id].session
        session = DisplaySession(
            session_id=session_id, display=self._free_display(), width=width, height=height
        )
        env = {"DISPLAY": session.display_name}
        processes = [
            self.runner.start(session.xvfb_argv(), env=env),
            self.runner.start(session.x11vnc_argv(), env=env),
            self.runner.start(session.novnc_argv(), env=env),
        ]
        self._live[session_id] = _Live(session, processes)
        metrics.set_gauge("slas_screen_displays_open", len(self._live))
        return session

    def close(self, session_id: str) -> None:
        live = self._live.pop(session_id, None)
        metrics.set_gauge("slas_screen_displays_open", len(self._live))
        if live is None:
            return
        for handle in reversed(live.processes):
            self.runner.stop(handle)

    def healthy(self, session_id: str) -> bool:
        live = self._live.get(session_id)
        return live is not None and all(self.runner.alive(p) for p in live.processes)

    def status_sentence(self) -> str:
        count = len(self._live)
        if count == 0:
            return f"No sessions; {self.max_displays} displays free."
        return f"{count} of {self.max_displays} displays in use."
