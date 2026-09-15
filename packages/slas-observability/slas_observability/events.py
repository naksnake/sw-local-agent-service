"""Structured JSON events with the trace id on every line (CLAUDE.md §11 "structlog JSON with
trace_id"). structlog is not an approved dependency yet; this is the same shape from the
standard library — one JSON object per line: ts, level, service, event, trace_id, fields —
so a later swap changes the sink, not the lines.

Nothing here redacts: callers pass `redact=` (the gateway's Redactor) when a field could
carry model or log text. Secrets never reach an event in the first place (INV-5).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

from slas_observability.tracing import current_trace_id

Level = Literal["debug", "info", "warning", "error"]


class Sink(Protocol):
    def write(self, line: str) -> None: ...


class ListSink:
    """Keeps every line; tests read them back as dicts."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]


class StreamSink:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stderr

    def write(self, line: str) -> None:
        self.stream.write(line + "\n")
        self.stream.flush()


class FileSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventLog:
    """One per service: `EventLog("llm-gateway", sink).info("completion", role="coder")`."""

    def __init__(
        self,
        service: str,
        sink: Sink,
        *,
        clock: Callable[[], datetime] = _utc_now,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.service = service
        self.sink = sink
        self._clock = clock
        self._redact = redact

    def emit(self, event: str, *, level: Level = "info", **fields: object) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "level": level,
            "service": self.service,
            "event": event,
            "trace_id": current_trace_id(),
        }
        for key, value in fields.items():
            if key in record:
                raise ValueError(f"{key} is a reserved event field.")
            record[key] = self._redact(value) if self._redact and isinstance(value, str) else value
        self.sink.write(json.dumps(record, ensure_ascii=False, default=str, sort_keys=False))
        return record

    def debug(self, event: str, **fields: object) -> dict[str, Any]:
        return self.emit(event, level="debug", **fields)

    def info(self, event: str, **fields: object) -> dict[str, Any]:
        return self.emit(event, level="info", **fields)

    def warning(self, event: str, **fields: object) -> dict[str, Any]:
        return self.emit(event, level="warning", **fields)

    def error(self, event: str, **fields: object) -> dict[str, Any]:
        return self.emit(event, level="error", **fields)


def trace_ids(records: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> set[str | None]:
    """The distinct trace ids in a batch of records: one id means one trace."""
    batch = [records] if isinstance(records, Mapping) else records
    return {record.get("trace_id") for record in batch}
