"""Watch and take over (CLAUDE.md §5.2, P10): the operator sees the station through VNC and
can pause the runner at any point, drive the station by hand, and resume or abort.

`Controller` holds the pause flag; `PausableScreen` checks it before every GUI action, so a
running skill stops at the next step boundary within a poll interval. While paused the runner
sends no input to the station; an abort stops the skill with a sentence the ticket keeps.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from slas_schemas.errors import ThreePartMessage
from slas_screen.driver import ScreenDriver
from slas_screen.model import ScreenResult, ScreenStopError
from slas_station_runner.protocol import ControlState


class Clock(Protocol):
    def now(self) -> datetime: ...


class Controller:
    def __init__(self, station: str, clock: Clock, *, poll_s: float = 0.05) -> None:
        self.station = station
        self.clock = clock
        self.poll_s = poll_s
        self._resume = threading.Event()
        self._resume.set()
        self._lock = threading.Lock()
        self.state = ControlState(station=station)
        self.waits = 0

    def pause(self, by: str) -> ControlState:
        with self._lock:
            self._resume.clear()
            self.state = ControlState(
                station=self.station, paused=True, by=by, since=self.clock.now()
            )
        return self.state

    def resume(self, by: str) -> ControlState:
        with self._lock:
            self.state = ControlState(station=self.station, by=by, since=self.clock.now())
            self._resume.set()
        return self.state

    def abort(self, by: str) -> ControlState:
        with self._lock:
            self.state = ControlState(
                station=self.station, aborted=True, by=by, since=self.clock.now()
            )
            self._resume.set()  # a paused step wakes up and sees the abort
        return self.state

    def status(self) -> ControlState:
        return self.state

    def checkpoint(self) -> None:
        """Called before every GUI action: block while paused, stop when aborted."""
        while not self._resume.is_set():
            self.waits += 1
            time.sleep(self.poll_s)
        if self.state.aborted:
            raise ScreenStopError(
                ThreePartMessage(
                    f"Stopped: {self.state.by or 'the operator'} took over {self.station}.",
                    "The operator aborted the run from the VNC view; the runner sent no more "
                    "input.",
                    "Finish by hand or start the job again once the station is back in a known "
                    "state.",
                )
            )


class PausableScreen(ScreenDriver):
    """A `ScreenDriver` that asks the controller before every primitive."""

    def __init__(self, *args: object, controller: Controller, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.controller = controller

    def _guarded(self, perform: Callable[[], ScreenResult]) -> ScreenResult:
        self.controller.checkpoint()
        return perform()

    def focus_window(
        self, *, title: str | None = None, wm_class: str | None = None
    ) -> ScreenResult:
        return self._guarded(
            lambda: super(PausableScreen, self).focus_window(title=title, wm_class=wm_class)
        )

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
        return self._guarded(
            lambda: super(PausableScreen, self).click(
                text=text, image=image, target=target, x=x, y=y, button=button, count=count
            )
        )

    def type_text(self, text: str) -> ScreenResult:
        return self._guarded(lambda: super(PausableScreen, self).type_text(text))

    def key(self, press: str) -> ScreenResult:
        return self._guarded(lambda: super(PausableScreen, self).key(press))

    def scroll(self, direction: str, amount: int) -> ScreenResult:
        return self._guarded(lambda: super(PausableScreen, self).scroll(direction, amount))

    def wait_for(
        self,
        *,
        window: str | None = None,
        text: str | None = None,
        image: str | None = None,
        timeout_s: float | None = None,
    ) -> ScreenResult:
        return self._guarded(
            lambda: super(PausableScreen, self).wait_for(
                window=window, text=text, image=image, timeout_s=timeout_s
            )
        )

    def assert_visible(
        self, *, text: str | None = None, image: str | None = None, message: str
    ) -> ScreenResult:
        return self._guarded(
            lambda: super(PausableScreen, self).assert_visible(
                text=text, image=image, message=message
            )
        )
