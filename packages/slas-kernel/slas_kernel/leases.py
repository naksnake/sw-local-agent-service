"""Exclusive leases on targets and stations (CLAUDE.md §10.2 guardrails, §10.3): one run per
machine at a time, durable in one JSON file so a restarted executor still knows who holds what."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage


class Lease(SlasModel):
    target: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    user: str = Field(min_length=1)
    since: datetime
    until: datetime

    def sentence(self) -> str:
        return (
            f"{self.target} is leased to {self.ticket_id} ({self.user}) "
            f"until {self.until:%Y-%m-%d %H:%M}."
        )


class LeaseError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class LeaseTable:
    """Leases in one JSON file so a restarted executor still knows who holds what."""

    def __init__(self, path: Path, *, noun: str = "target") -> None:
        self.path = path
        self.noun = noun

    def _load(self) -> dict[str, Lease]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {target: Lease.model_validate(item) for target, item in raw.items()}

    def _save(self, items: dict[str, Lease]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            self.path,
            json.dumps({t: lease.model_dump(mode="json") for t, lease in items.items()}, indent=2)
            + "\n",
            mode=0o600,
        )

    def holder(self, target: str, now: datetime) -> Lease | None:
        lease = self._load().get(target)
        if lease is None or lease.until <= now:
            return None
        return lease

    def acquire(
        self, target: str, *, ticket_id: str, user: str, now: datetime, max_hours: int
    ) -> Lease:
        items = self._load()
        current = items.get(target)
        if current is not None and current.until > now and current.ticket_id != ticket_id:
            raise LeaseError(
                ThreePartMessage(
                    f"{target} is busy: {current.sentence()}",
                    f"Only one run may use a {self.noun} at a time (exclusive lease).",
                    f"Pick another {self.noun}, or wait for that run to finish or its lease to "
                    "expire.",
                )
            )
        lease = Lease(
            target=target,
            ticket_id=ticket_id,
            user=user,
            since=current.since if current and current.ticket_id == ticket_id else now,
            until=now + timedelta(hours=max_hours),
        )
        items[target] = lease
        self._save(items)
        return lease

    def release(self, target: str, *, ticket_id: str) -> bool:
        items = self._load()
        current = items.get(target)
        if current is None or current.ticket_id != ticket_id:
            return False
        del items[target]
        self._save(items)
        return True

    def free_targets(self, candidates: list[str], now: datetime) -> list[str]:
        return [t for t in candidates if self.holder(t, now) is None]
