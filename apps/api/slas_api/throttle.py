"""Sign-in throttling: 10 failures per 15 minutes per email and per address (ADR-0007).

Counters live in Redis and the check fails closed: when Redis does not answer, sign-in
answers 503 with the "Rate limiter down" sentences rather than letting an attacker through
during an outage. `MemoryThrottle` is the same protocol for tests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

import redis
from redis.exceptions import RedisError

FAILURE_LIMIT: Final = 10
WINDOW: Final = timedelta(minutes=15)
_KEY_PREFIX: Final = "slas:signin:fail"


class ThrottleUnavailableError(RuntimeError):
    """The service that counts attempts did not answer; the caller fails closed."""


class Throttle(Protocol):
    def is_blocked(self, email: str, address: str) -> bool: ...

    def record_failure(self, email: str, address: str) -> None: ...

    def clear(self, email: str) -> None: ...

    def ping(self) -> bool: ...


def _keys(email: str, address: str) -> tuple[str, str]:
    email_digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:email:{email_digest}", f"{_KEY_PREFIX}:addr:{address}"


class RedisThrottle:
    def __init__(
        self,
        host: str,
        port: int,
        password: str | None,
        *,
        socket_timeout: float = 2.0,
    ) -> None:
        self._client = redis.Redis(
            host=host,
            port=port,
            password=password,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
        )

    def is_blocked(self, email: str, address: str) -> bool:
        try:
            values = self._client.mget(list(_keys(email, address)))
        except (RedisError, OSError) as exc:
            raise ThrottleUnavailableError(str(exc)) from exc
        counts = [int(value) for value in values if value is not None]
        return any(count >= FAILURE_LIMIT for count in counts)

    def record_failure(self, email: str, address: str) -> None:
        window_s = int(WINDOW.total_seconds())
        try:
            pipe = self._client.pipeline()
            for key in _keys(email, address):
                pipe.incr(key)
                pipe.expire(key, window_s, nx=True)
            pipe.execute()
        except (RedisError, OSError) as exc:
            raise ThrottleUnavailableError(str(exc)) from exc

    def clear(self, email: str) -> None:
        email_key, _ = _keys(email, "-")
        try:
            self._client.delete(email_key)
        except (RedisError, OSError) as exc:
            raise ThrottleUnavailableError(str(exc)) from exc

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except (RedisError, OSError):
            return False


class MemoryThrottle:
    """The same rules in memory, with an injectable clock and an `available` switch."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._counts: dict[str, list[datetime]] = {}
        self.available = True

    def _check(self) -> None:
        if not self.available:
            raise ThrottleUnavailableError("the in-memory throttle is switched off")

    def _live(self, key: str) -> list[datetime]:
        cutoff = self._clock() - WINDOW
        kept = [at for at in self._counts.get(key, []) if at > cutoff]
        self._counts[key] = kept
        return kept

    def is_blocked(self, email: str, address: str) -> bool:
        self._check()
        return any(len(self._live(key)) >= FAILURE_LIMIT for key in _keys(email, address))

    def record_failure(self, email: str, address: str) -> None:
        self._check()
        now = self._clock()
        for key in _keys(email, address):
            self._live(key).append(now)

    def clear(self, email: str) -> None:
        self._check()
        email_key, _ = _keys(email, "-")
        self._counts.pop(email_key, None)

    def ping(self) -> bool:
        return self.available
