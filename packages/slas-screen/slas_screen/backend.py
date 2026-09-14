"""The backend boundary: what PyAutoGUI + xdotool provide, and the scripted fake for tests.

The fake replays windows and visible texts; every action is recorded; it can change the
focused window behind the driver's back to exercise the hard stop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from slas_screen.model import Point, Window

#: A 1x1 transparent PNG, so fake screenshots are real image files.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


class ScreenBackend(Protocol):
    def windows(self) -> list[Window]: ...

    def focused(self) -> Window | None: ...

    def activate(self, window: Window) -> None: ...

    def find_text(self, text: str) -> Point | None: ...

    def find_image(self, image: str) -> Point | None: ...

    def find_target(self, target: str) -> Point | None: ...

    def click_at(self, point: Point, *, button: str, count: int) -> None: ...

    def type_text(self, text: str) -> None: ...

    def press(self, keys: str) -> None: ...

    def scroll(self, direction: str, amount: int) -> None: ...

    def screenshot(self, path: Path) -> None: ...


class FakeScreen:
    def __init__(self) -> None:
        self._windows: list[Window] = []
        self._focused: Window | None = None
        self.visible_texts: set[str] = set()
        self.images: dict[str, Point] = {}
        self.targets: dict[str, Point] = {}
        self.actions: list[str] = []
        self.typed: list[str] = []
        self.screenshots: list[Path] = []
        #: Scripted surprises: after this many actions, focus jumps to the named window.
        self.steal_focus_after: tuple[int, str] | None = None
        #: Texts that become visible after N `find_text` polls (to exercise wait_for).
        self.appear_after: dict[str, int] = {}
        self._polls: dict[str, int] = {}

    # --- scripting -----------------------------------------------------------------------

    def add_window(
        self, window_id: str, title: str, wm_class: str = "", *, focused: bool = False
    ) -> Window:
        window = Window(id=window_id, title=title, wm_class=wm_class)
        self._windows.append(window)
        if focused or self._focused is None:
            self._focused = window
        return window

    def show_text(self, *texts: str) -> None:
        self.visible_texts.update(texts)

    def hide_text(self, *texts: str) -> None:
        self.visible_texts.difference_update(texts)

    def _maybe_steal_focus(self) -> None:
        if self.steal_focus_after is None:
            return
        after, title = self.steal_focus_after
        if len(self.actions) >= after:
            thief = next((w for w in self._windows if w.title == title), None)
            if thief is None:
                thief = self.add_window(f"thief-{len(self._windows)}", title)
            self._focused = thief
            self.steal_focus_after = None

    # --- ScreenBackend -------------------------------------------------------------------

    def windows(self) -> list[Window]:
        return list(self._windows)

    def focused(self) -> Window | None:
        return self._focused

    def activate(self, window: Window) -> None:
        self.actions.append(f"activate {window.id}")
        self._focused = window
        self._maybe_steal_focus()

    def find_text(self, text: str) -> Point | None:
        polls = self._polls.get(text, 0) + 1
        self._polls[text] = polls
        if text in self.appear_after and polls > self.appear_after[text]:
            self.visible_texts.add(text)
        return Point(x=100, y=100) if text in self.visible_texts else None

    def find_image(self, image: str) -> Point | None:
        return self.images.get(image)

    def find_target(self, target: str) -> Point | None:
        return self.targets.get(target)

    def click_at(self, point: Point, *, button: str, count: int) -> None:
        self.actions.append(f"click {button}x{count} at {point.x},{point.y}")
        self._maybe_steal_focus()

    def type_text(self, text: str) -> None:
        self.actions.append("type")
        self.typed.append(text)
        self._maybe_steal_focus()

    def press(self, keys: str) -> None:
        self.actions.append(f"key {keys}")
        self._maybe_steal_focus()

    def scroll(self, direction: str, amount: int) -> None:
        self.actions.append(f"scroll {direction} {amount}")
        self._maybe_steal_focus()

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(TINY_PNG)
        self.screenshots.append(path)
