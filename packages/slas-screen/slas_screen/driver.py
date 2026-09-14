"""The screen driver: the deterministic executor of GUI steps (CLAUDE.md §5.2, INV-3).

Rules enforced here, whatever the backend:
- a screenshot before and after every step, both returned for the ticket;
- targets by window title, accessible name or template image; bare coordinates only when
  the recipe author wrote them;
- at most N actions per second;
- a deny-list of windows (terminal emulators on the platform host, password managers);
- a hard stop if the focused window changes unexpectedly.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from slas_schemas.errors import ThreePartMessage
from slas_screen.backend import ScreenBackend
from slas_screen.model import Point, ScreenResult, ScreenStopError, Window
from slas_screen.policy import ScreenPolicy


class Clock(Protocol):
    def now(self) -> datetime: ...


class RateLimiter:
    """Never more than `per_second` actions in any rolling second; sleeps otherwise."""

    def __init__(self, clock: Clock, sleep: Callable[[float], None], per_second: int) -> None:
        self._clock = clock
        self._sleep = sleep
        self.per_second = per_second
        self._stamps: deque[datetime] = deque()
        self.waits = 0

    def acquire(self) -> None:
        now = self._clock.now()
        window = timedelta(seconds=1)
        while self._stamps and now - self._stamps[0] >= window:
            self._stamps.popleft()
        if len(self._stamps) >= self.per_second:
            wait = (self._stamps[0] + window - now).total_seconds()
            self.waits += 1
            self._sleep(max(wait, 0.001))
            now = self._clock.now()
            while self._stamps and now - self._stamps[0] >= window:
                self._stamps.popleft()
        self._stamps.append(now)


class ScreenDriver:
    def __init__(
        self,
        backend: ScreenBackend,
        *,
        policy: ScreenPolicy,
        clock: Clock,
        screenshots_dir: Path,
        sleep: Callable[[float], None],
    ) -> None:
        self.backend = backend
        self.policy = policy
        self.clock = clock
        self.screenshots_dir = screenshots_dir
        self._sleep = sleep
        self.limiter = RateLimiter(clock, sleep, policy.max_actions_per_second)
        self._expected_focus: Window | None = None
        self._shot_counter = 0
        self.actions = 0

    # --- primitives ------------------------------------------------------------------------

    def focus_window(
        self, *, title: str | None = None, wm_class: str | None = None
    ) -> ScreenResult:
        before = self._shoot("focus-before")
        match = self._find_window(title, wm_class)
        if match is None:
            wanted = title or wm_class or "?"
            return self._result(False, f"No window called {wanted!r} is open.", before)
        self._deny_check(match)
        self._action(lambda: self.backend.activate(match))
        self._expected_focus = match
        return self._result(True, f"Focused {match.title!r}.", before, matched=match.title)

    def click(
        self,
        *,
        text: str | None = None,
        image: str | None = None,
        target: str | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        count: int = 1,
    ) -> ScreenResult:
        before = self._shoot("click-before")
        point, label = self._locate(text=text, image=image, target=target, x=x, y=y)
        if point is None:
            return self._result(False, f"Could not find {label} on the screen.", before)
        self._guard_focus()
        self._action(lambda: self.backend.click_at(point, button=button, count=count))
        verb = {1: "Clicked", 2: "Double-clicked"}.get(count, f"Clicked {count} times")
        if button == "right":
            verb = "Right-clicked"
        return self._result(True, f"{verb} {label}.", before, matched=label)

    def type_text(self, text: str) -> ScreenResult:
        before = self._shoot("type-before")
        self._guard_focus()
        self._action(lambda: self.backend.type_text(text))
        return self._result(True, f"Typed {len(text)} characters.", before)

    def key(self, press: str) -> ScreenResult:
        before = self._shoot("key-before")
        self._guard_focus()
        self._action(lambda: self.backend.press(press))
        return self._result(True, f"Pressed {press}.", before)

    def scroll(self, direction: str, amount: int) -> ScreenResult:
        before = self._shoot("scroll-before")
        if direction not in ("up", "down", "left", "right"):
            return self._result(False, f"{direction!r} is not a scroll direction.", before)
        self._guard_focus()
        self._action(lambda: self.backend.scroll(direction, amount))
        return self._result(True, f"Scrolled {direction} by {amount}.", before)

    def wait_for(
        self,
        *,
        window: str | None = None,
        text: str | None = None,
        image: str | None = None,
        timeout_s: float | None = None,
    ) -> ScreenResult:
        before = self._shoot("wait-before")
        timeout = timeout_s if timeout_s is not None else self.policy.default_timeout_s
        timeout *= self.policy.wait_timeout_scale
        deadline = self.clock.now() + timedelta(seconds=timeout)
        wanted = window or text or image or "?"
        while True:
            if window is not None:
                found = self._find_window(window, None)
                if found is not None:
                    self._deny_check(found)
                    self._expected_focus = self.backend.focused() or found
                    return self._result(
                        True, f"{found.title!r} appeared.", before, matched=found.title
                    )
            elif text is not None and self.backend.find_text(text) is not None:
                return self._result(True, f"{text!r} appeared.", before, matched=text)
            elif image is not None and self.backend.find_image(image) is not None:
                return self._result(True, f"Image {image} appeared.", before, matched=image)
            if self.clock.now() >= deadline:
                return self._result(
                    False, f"{wanted!r} did not appear within {timeout:g} seconds.", before
                )
            self._sleep(self.policy.poll_interval_s)

    def screenshot(self, name: str) -> ScreenResult:
        path = self._shoot(name)
        return ScreenResult(ok=True, sentence=f"Screenshot {name} taken.", before=None, after=path)

    def assert_visible(
        self, *, text: str | None = None, image: str | None = None, message: str
    ) -> ScreenResult:
        before = self._shoot("assert-before")
        visible = (text is not None and self.backend.find_text(text) is not None) or (
            image is not None and self.backend.find_image(image) is not None
        )
        if visible:
            return self._result(True, f"Visible: {message}.", before, matched=text or image)
        return self._result(False, f"Not visible: {message}.", before)

    # --- internals ---------------------------------------------------------------------------

    def _shoot(self, label: str) -> str:
        self._shot_counter += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "shot"
        path = self.screenshots_dir / f"{self._shot_counter:04d}-{safe}.png"
        self.backend.screenshot(path)
        return str(path)

    def _result(
        self, ok: bool, sentence: str, before: str, *, matched: str | None = None
    ) -> ScreenResult:
        after = self._shoot("after")
        return ScreenResult(ok=ok, sentence=sentence, before=before, after=after, matched=matched)

    def _action(self, perform: Callable[[], None]) -> None:
        self.limiter.acquire()
        self.actions += 1
        perform()
        if self.policy.action_settle_s:
            self._sleep(self.policy.action_settle_s)

    def _find_window(self, title: str | None, wm_class: str | None) -> Window | None:
        for window in self.backend.windows():
            if title is not None and self.policy.title_matches(title, window.title):
                return window
            if wm_class is not None and wm_class.lower() == window.wm_class.lower():
                return window
        return None

    def _deny_check(self, window: Window) -> None:
        pattern = self.policy.denied(window.title, window.wm_class)
        if pattern is not None:
            raise ScreenStopError(
                ThreePartMessage(
                    f"Stopped: the window {window.title!r} is on the deny-list.",
                    "Terminal emulators on the platform host and password managers are never "
                    "driven by a skill (CLAUDE.md §5.2, INV-4).",
                    "Point the step at the application window instead.",
                )
            )

    def _guard_focus(self) -> None:
        current = self.backend.focused()
        if current is not None:
            self._deny_check(current)
        if self._expected_focus is None:
            self._expected_focus = current
            return
        if current is not None and current.id != self._expected_focus.id:
            expected = self._expected_focus
            self._expected_focus = None
            raise ScreenStopError(
                ThreePartMessage(
                    f"Stopped: the focused window changed from {expected.title!r} to "
                    f"{current.title!r} unexpectedly.",
                    "Something else took the focus, so the next keystrokes would go to the wrong "
                    "window.",
                    "Check the screenshots, bring the right window back and resume.",
                )
            )

    def _locate(
        self,
        *,
        text: str | None,
        image: str | None,
        target: str | None,
        x: int | None,
        y: int | None,
    ) -> tuple[Point | None, str]:
        if text is not None:
            return self.backend.find_text(text), f"the text {text!r}"
        if target is not None:
            return self.backend.find_target(target), f"the target {target}"
        if image is not None:
            return self.backend.find_image(image), f"the image {image}"
        if x is not None and y is not None:
            if not self.policy.allow_coordinates:
                raise ScreenStopError(
                    ThreePartMessage(
                        "Stopped: this step clicks bare coordinates, which this installation "
                        "does not allow.",
                        "Targets are found by window title, accessible name or template image.",
                        "Give the step a text, target or image instead.",
                    )
                )
            return Point(x=x, y=y), f"({x}, {y})"
        return None, "a target"
