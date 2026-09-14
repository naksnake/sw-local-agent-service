"""A circuit breaker per model instance (CLAUDE.md §4.2 "breaker", P3 done-when).

Two invalid answers in a row pause a voter for a cooldown; after the cooldown one trial
request is let through (half-open) and its result closes or reopens the breaker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Entry:
    failures: int = 0
    state: BreakerState = BreakerState.CLOSED
    opened_at: datetime | None = None
    last_reason: str = ""


class CircuitBreaker:
    def __init__(
        self,
        clock: Clock,
        *,
        failure_threshold: int = 2,
        cooldown: timedelta = timedelta(minutes=5),
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._clock = clock
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._entries: dict[str, _Entry] = {}

    def _entry(self, key: str) -> _Entry:
        return self._entries.setdefault(key, _Entry())

    def state(self, key: str) -> BreakerState:
        entry = self._entry(key)
        if (
            entry.state is BreakerState.OPEN
            and entry.opened_at is not None
            and self._clock.now() >= entry.opened_at + self.cooldown
        ):
            entry.state = BreakerState.HALF_OPEN
        return entry.state

    def allow(self, key: str) -> bool:
        """True if a request may go to `key` now. Half-open lets exactly one trial through."""
        state = self.state(key)
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        entry = self._entry(key)
        if entry.last_reason == "trial in flight":
            return False
        entry.last_reason = "trial in flight"
        return True

    def record_success(self, key: str) -> None:
        self._entries[key] = _Entry()

    def record_failure(self, key: str, reason: str = "invalid answer") -> None:
        entry = self._entry(key)
        entry.failures += 1
        entry.last_reason = reason
        if entry.state is BreakerState.HALF_OPEN or entry.failures >= self.failure_threshold:
            entry.state = BreakerState.OPEN
            entry.opened_at = self._clock.now()

    def failures(self, key: str) -> int:
        return self._entry(key).failures

    def paused_until(self, key: str) -> datetime | None:
        entry = self._entry(key)
        if self.state(key) is BreakerState.OPEN and entry.opened_at is not None:
            return entry.opened_at + self.cooldown
        return None

    def open_keys(self) -> list[str]:
        return sorted(key for key in self._entries if self.state(key) is BreakerState.OPEN)

    def sentence(self, key: str) -> str:
        state = self.state(key)
        entry = self._entry(key)
        if state is BreakerState.CLOSED:
            return f"{key} is answering normally."
        if state is BreakerState.HALF_OPEN:
            return f"{key} is being tried again after a pause."
        until = self.paused_until(key)
        when = until.strftime("%H:%M") if until else "later"
        noun = "invalid answer" if entry.failures == 1 else "invalid answers"
        return f"{key} is paused until {when} after {entry.failures} {noun}."
