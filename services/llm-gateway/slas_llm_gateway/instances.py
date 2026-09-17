"""The instance table: which vLLM instance answers where (docs/api-contract-round-2.md §2).

The model manager owns every `vllm-*` container and announces them with
`PUT /v1/instances`; the gateway keeps only this table and asks it at call time. Nothing is
read from `Models/models.yaml` here: routes and instances arrive over the wire, so the
gateway is not tied to the registry's layout (INV-9: a swap is one PUT, never a restart).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from pydantic import Field, field_validator

from slas_llm_gateway.vllm import InstanceUnavailableError, ResolvedInstance
from slas_schemas.common import SlasModel


class Clock(Protocol):
    def now(self) -> datetime: ...


class InstanceAnnouncement(SlasModel):
    """One entry of the model manager's `PUT /v1/instances` body."""

    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    url: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    healthy: bool = True

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")


class InstanceRecord(InstanceAnnouncement):
    last_seen: datetime

    def view(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "model_id": self.model_id,
            "healthy": self.healthy,
            "last_seen": self.last_seen.isoformat(),
        }


def not_registered(instance: str) -> str:
    return (
        f"No healthy instance called {instance} has been announced to the gateway; "
        "the model manager is still starting it."
    )


class InstanceTable:
    """instance name → record. `replace()` swaps the whole table, as the contract says."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, InstanceRecord] = {}

    def replace(self, announcements: Iterable[InstanceAnnouncement]) -> list[str]:
        """Install the announced instances; returns the names that just became healthy."""
        now = self._clock.now()
        fresh = {
            item.name: InstanceRecord(**item.model_dump(), last_seen=now) for item in announcements
        }
        with self._lock:
            previous = self._records
            self._records = fresh
        return sorted(
            name
            for name, record in fresh.items()
            if record.healthy and not (name in previous and previous[name].healthy)
        )

    def get(self, name: str) -> InstanceRecord | None:
        return self._records.get(name)

    def is_healthy(self, name: str) -> bool:
        record = self.get(name)
        return record is not None and record.healthy

    def names(self) -> list[str]:
        return sorted(self._records)

    def empty(self) -> bool:
        return not self._records

    def resolve(self, instance: str) -> ResolvedInstance:
        record = self.get(instance)
        if record is None or not record.healthy:
            raise InstanceUnavailableError(instance, not_registered(instance))
        return ResolvedInstance(url=record.url, model=record.model_id)

    def view(self) -> dict[str, dict[str, Any]]:
        """The `instances` object of `GET /v1/routes`."""
        return {name: self._records[name].view() for name in self.names()}
