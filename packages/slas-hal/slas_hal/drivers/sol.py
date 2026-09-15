"""Serial-over-LAN capture: a long-lived `ipmitool sol activate`, drained line by line into
memory and `Validation/Console/<alias>.log`. Fence markers are appended as our own
annotations so the console file shows exactly where each power action sits (INV-6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from slas_hal.drivers.process import StreamHandle, StreamRunner


class Clock(Protocol):
    def now(self) -> datetime: ...


class SolCapture:
    def __init__(
        self,
        alias: str,
        *,
        runner: StreamRunner,
        argv: Sequence[str],
        env: Mapping[str, str],
        sink: Path,
        clock: Clock,
    ) -> None:
        self.alias = alias
        self.runner = runner
        self.argv = list(argv)
        self.env = dict(env)
        self.sink = sink
        self.clock = clock
        self.lines_seen: list[str] = []
        self._handle: StreamHandle | None = None

    @property
    def active(self) -> bool:
        return self._handle is not None and self._handle.running()

    def start(self) -> None:
        if self._handle is not None:
            return
        self.sink.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.runner.start(self.argv, env=self.env)
        self._write(f"--- slas sol capture started {self.clock.now().isoformat()} ---")

    def drain(self, *, max_lines: int = 10_000) -> int:
        """Pull every line the process has produced so far. Returns how many arrived."""
        if self._handle is None:
            return 0
        arrived = 0
        while arrived < max_lines:
            line = self._handle.readline(0.05)
            if line is None:
                break
            self._write(line)
            arrived += 1
        return arrived

    def annotate(self, marker: str) -> None:
        self.drain()
        self._write(marker)

    def lines(self, *, since: int = 0) -> list[str]:
        self.drain()
        return list(self.lines_seen[since:])

    def stop(self) -> None:
        if self._handle is None:
            return
        self.drain()
        self._handle.stop()
        self._write(f"--- slas sol capture stopped {self.clock.now().isoformat()} ---")
        self._handle = None

    def _write(self, line: str) -> None:
        self.lines_seen.append(line)
        with self.sink.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
