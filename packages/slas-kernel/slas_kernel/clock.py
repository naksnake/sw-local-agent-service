"""Time as a dependency, so tests are deterministic and journals are reproducible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Starts at a fixed instant and advances a fixed step on every call."""

    def __init__(
        self,
        start: datetime | None = None,
        step: timedelta = timedelta(seconds=1),
    ) -> None:
        self._now = start or datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
        self._step = step

    def now(self) -> datetime:
        current = self._now
        self._now = current + self._step
        return current
