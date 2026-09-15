"""A syslog receiver for the targets' remote logging (CLAUDE.md §12 `SYSLOG_LISTEN`).

UDP, RFC 3164 (`<13>Sep 14 08:00:00 host tag: message`) and RFC 5424
(`<13>1 2026-09-14T08:00:00Z host app - - - message`), one JSON line per message under
`Validation/Syslog/<listen>.jsonl`. Standard library only; binds where the compose file says.
"""

from __future__ import annotations

import json
import re
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import Field

from slas_schemas.common import SlasModel

_RFC3164: Final = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<ts>[A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2}) "
    r"(?P<host>\S+) (?P<msg>.*)$",
    re.S,
)
_RFC5424: Final = re.compile(
    r"^<(?P<pri>\d{1,3})>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) \S+ \S+ "
    r"(?:-|\[[^\]]*\]) ?(?P<msg>.*)$",
    re.S,
)


class SyslogLine(SlasModel):
    received_at: datetime
    source: str
    host: str = "-"
    severity: int = Field(default=6, ge=0, le=7)
    facility: int = Field(default=1, ge=0, le=23)
    message: str

    def line(self) -> str:
        return f"[{self.received_at.isoformat()}] {self.host}: {self.message}"


def parse_syslog(text: str, *, source: str, received_at: datetime) -> SyslogLine:
    for pattern in (_RFC5424, _RFC3164):
        match = pattern.match(text)
        if match is not None:
            pri = int(match.group("pri"))
            message = match.group("msg").strip()
            return SyslogLine(
                received_at=received_at,
                source=source,
                host=match.group("host"),
                severity=pri & 7,
                facility=min(pri >> 3, 23),
                message=message,
            )
    return SyslogLine(received_at=received_at, source=source, message=text.strip())


class SyslogReceiver:
    def __init__(self, *, listen: str = "0.0.0.0:5514", sink: Path) -> None:
        host, _, port = listen.rpartition(":")
        self.host = host or "0.0.0.0"  # noqa: S104 — the lab VLAN listener, per compose
        self.requested_port = int(port)
        self.sink = sink
        self.lines_seen: list[SyslogLine] = []
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._socket is None:
            return self.requested_port
        return int(self._socket.getsockname()[1])

    def start(self) -> None:
        if self._socket is not None:
            return
        self.sink.parent.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.requested_port))
        sock.settimeout(0.2)
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="slas-syslog", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._socket is not None  # noqa: S101 — set by start()
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            line = parse_syslog(
                text, source=f"{address[0]}:{address[1]}", received_at=datetime.now(UTC)
            )
            self._record(line)

    def _record(self, line: SyslogLine) -> None:
        with self._lock:
            self.lines_seen.append(line)
            self.sink.parent.mkdir(parents=True, exist_ok=True)
            with self.sink.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line.model_dump(mode="json")) + "\n")

    def annotate(self, marker: str, *, now: datetime) -> None:
        self._record(SyslogLine(received_at=now, source="slas", host="slas", message=marker))

    def lines(self, *, since: int = 0) -> list[SyslogLine]:
        with self._lock:
            return list(self.lines_seen[since:])

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
