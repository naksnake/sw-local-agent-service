"""The xdotool backend: argv only, text over stdin, screenshots with ImageMagick's `import`.

Runs inside the screen worker against the platform's own Xvfb display (ADR-0002); never
against the host display. Text and image search need PyAutoGUI (an approved dependency
away) or an accessibility bridge; until then those return None and the driver reports
"could not find", so a recipe fails loudly instead of clicking blind.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from slas_screen.model import Point, Window


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int | None
    stdout: str


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, stdin_text: str | None = None) -> CommandResult: ...


@dataclass
class FakeCommandRunner:
    outputs: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, stdin_text: str | None = None) -> CommandResult:
        key = tuple(argv)
        self.calls.append((key, stdin_text))
        return self.outputs.get(key, CommandResult(0, ""))


_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
_SCROLL = {"up": "4", "down": "5", "left": "6", "right": "7"}


class XdotoolBackend:
    def __init__(self, runner: CommandRunner, *, display: str) -> None:
        self.runner = runner
        self.display = display

    def _x(self, *argv: str, stdin_text: str | None = None) -> CommandResult:
        return self.runner.run(["xdotool", *argv], stdin_text=stdin_text)

    def windows(self) -> list[Window]:
        ids = self._x("search", "--onlyvisible", "--name", ".").stdout.split()
        windows: list[Window] = []
        for window_id in ids:
            title = self._x("getwindowname", window_id).stdout.strip()
            wm_class = self._x("getwindowclassname", window_id).stdout.strip()
            windows.append(Window(id=window_id, title=title, wm_class=wm_class))
        return windows

    def focused(self) -> Window | None:
        result = self._x("getactivewindow")
        if not result.stdout.strip():
            return None
        window_id = result.stdout.strip()
        return Window(
            id=window_id,
            title=self._x("getwindowname", window_id).stdout.strip(),
            wm_class=self._x("getwindowclassname", window_id).stdout.strip(),
        )

    def activate(self, window: Window) -> None:
        self._x("windowactivate", "--sync", window.id)

    def find_text(self, text: str) -> Point | None:
        # TODO(SLAS-SCREEN): text search needs PyAutoGUI/OCR or an accessibility bridge.
        return None

    def find_image(self, image: str) -> Point | None:
        # TODO(SLAS-SCREEN): template matching arrives with PyAutoGUI (locateOnScreen).
        return None

    def find_target(self, target: str) -> Point | None:
        # TODO(SLAS-SCREEN): accessible names need an AT-SPI bridge inside the worker.
        return None

    def click_at(self, point: Point, *, button: str, count: int) -> None:
        self._x("mousemove", "--sync", str(point.x), str(point.y))
        self._x("click", "--repeat", str(count), "--delay", "80", _BUTTONS[button])

    def type_text(self, text: str) -> None:
        # Text goes over stdin, never argv: a secret must not show in the process list.
        self._x("type", "--delay", "20", "--file", "-", stdin_text=text)

    def press(self, keys: str) -> None:
        self._x("key", "--clearmodifiers", keys)

    def scroll(self, direction: str, amount: int) -> None:
        self._x("click", "--repeat", str(max(1, amount)), "--delay", "30", _SCROLL[direction])

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.runner.run(["import", "-display", self.display, "-window", "root", str(path)])
