"""Host probing behind one interface so `slas doctor` is testable against a fake.

`RealHostProbe` touches the host; `slas_cli.fakes.FakeHostProbe` replays canned answers.
Every command is an argv list with a timeout; nothing is ever joined into a shell line
(CLAUDE.md §11).
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class HostProbe(Protocol):
    def os_description(self) -> str: ...

    def kernel_release(self) -> str: ...

    def cpu_count(self) -> int: ...

    def memory_total_bytes(self) -> int | None: ...

    def path_exists(self, path: str) -> bool: ...

    def path_writable(self, path: str) -> bool: ...

    def disk_free_bytes(self, path: str) -> int | None: ...

    def which(self, executable: str) -> str | None: ...

    def run(self, argv: Sequence[str], timeout_s: float = 10.0) -> CommandResult | None: ...

    def port_free(self, port: int) -> bool: ...

    def python_version(self) -> tuple[int, int, int]: ...


def _nearest_existing(path: str) -> Path:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


class RealHostProbe:
    """Reads the real host. Stateless; safe to call repeatedly."""

    def os_description(self) -> str:
        try:
            release = platform.freedesktop_os_release()
            return str(release.get("PRETTY_NAME") or release.get("NAME") or platform.platform())
        except OSError:
            return platform.platform()

    def kernel_release(self) -> str:
        return platform.release()

    def cpu_count(self) -> int:
        return os.cpu_count() or 1

    def memory_total_bytes(self) -> int | None:
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        try:
            return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        except (ValueError, OSError, AttributeError):
            return None

    def path_exists(self, path: str) -> bool:
        return Path(path).exists()

    def path_writable(self, path: str) -> bool:
        return os.access(_nearest_existing(path), os.W_OK)

    def disk_free_bytes(self, path: str) -> int | None:
        try:
            return shutil.disk_usage(_nearest_existing(path)).free
        except OSError:
            return None

    def which(self, executable: str) -> str | None:
        return shutil.which(executable)

    def run(self, argv: Sequence[str], timeout_s: float = 10.0) -> CommandResult | None:
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell, fixed timeout
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))  # noqa: S104 - a bind probe, closed immediately
            except OSError:
                return False
            return True

    def python_version(self) -> tuple[int, int, int]:
        v = sys.version_info
        return (v.major, v.minor, v.micro)
