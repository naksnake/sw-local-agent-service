"""How `slas doctor` looks at the host.

`Host` is the only way a check touches the machine: no check calls `subprocess`, `os`,
`shutil` or `socket` directly. `RealHost` is the production implementation; the scripted
fake for tests lives in `fakes.py`. Everything here is read-only — the preflight never
changes the host.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of one command. `returncode` is None when the command could not start."""

    returncode: int | None
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Host(Protocol):
    """Read-only questions a check may ask about the machine."""

    def system(self) -> str: ...
    def kernel_release(self) -> str: ...
    def machine(self) -> str: ...
    def cpu_count(self) -> int | None: ...
    def memory_total_bytes(self) -> int | None: ...
    def which(self, command: str) -> str | None: ...
    def run(self, argv: Sequence[str], timeout_s: float = 10.0) -> CommandResult: ...
    def path_exists(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
    def is_writable(self, path: str) -> bool: ...
    def disk_free_bytes(self, path: str) -> int | None: ...
    def read_text(self, path: str) -> str | None: ...
    def port_in_use(self, port: int) -> bool | None: ...
    def env(self, name: str) -> str | None: ...


class RealHost:
    """Probes the machine the preflight runs on. Changes nothing."""

    def system(self) -> str:
        return platform.system()

    def kernel_release(self) -> str:
        return platform.release()

    def machine(self) -> str:
        return platform.machine()

    def cpu_count(self) -> int | None:
        return os.cpu_count()

    def memory_total_bytes(self) -> int | None:
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            return None
        if pages <= 0 or page_size <= 0:
            return None
        return pages * page_size

    def which(self, command: str) -> str | None:
        return shutil.which(command)

    def run(self, argv: Sequence[str], timeout_s: float = 10.0) -> CommandResult:
        """Run a fixed argv list with no shell, no stdin and a hard timeout."""
        if not argv:
            return CommandResult(None, "")
        executable = shutil.which(argv[0])
        if executable is None:
            return CommandResult(None, "")
        try:
            completed = subprocess.run(  # noqa: S603 — argv list, shell=False, fixed commands
                [executable, *argv[1:]],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return CommandResult(None, "")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def path_exists(self, path: str) -> bool:
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_writable(self, path: str) -> bool:
        return os.access(path, os.W_OK | os.X_OK)

    def disk_free_bytes(self, path: str) -> int | None:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return None

    def port_in_use(self, port: int) -> bool | None:
        """True if something answers on localhost:port, False if refused, None if unknown."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except ConnectionRefusedError:
            return False
        except OSError:
            return None

    def env(self, name: str) -> str | None:
        return os.environ.get(name)
