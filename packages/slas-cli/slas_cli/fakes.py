"""A scripted host for tests (CLAUDE.md §0.3: tests run against fakes, never a real host)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from slas_cli.probe import CommandResult

GIB = 1024**3


@dataclass
class FakeHostProbe:
    """Answers every probe question from its fields.

    `commands` maps an argv prefix (as a tuple) to the result `run()` returns; an argv with
    no matching prefix returns None, which the doctor treats as "did not answer".
    """

    os_text: str = "Ubuntu 24.04.1 LTS"
    kernel: str = "6.8.0-45-generic"
    cpus: int = 32
    memory_bytes: int | None = 256 * GIB
    existing_paths: set[str] = field(default_factory=lambda: {"/", "/AI", "/AI/Agent"})
    writable: bool = True
    free_bytes: int | None = 2000 * GIB
    executables: dict[str, str] = field(
        default_factory=lambda: {
            "podman": "/usr/bin/podman",
            "nvidia-smi": "/usr/bin/nvidia-smi",
            "runsc": "/usr/local/bin/runsc",
        }
    )
    commands: dict[tuple[str, ...], CommandResult] = field(
        default_factory=lambda: {
            ("podman", "--version"): CommandResult(0, "podman version 5.2.3\n", ""),
            ("podman", "compose", "version"): CommandResult(
                0, "podman-compose version 1.2.0\n", ""
            ),
            ("nvidia-smi",): CommandResult(
                0,
                "NVIDIA H100 80GB HBM3, 81559, 550.90.07\n"
                "NVIDIA H100 80GB HBM3, 81559, 550.90.07\n",
                "",
            ),
        }
    )
    busy_ports: set[int] = field(default_factory=set)
    python: tuple[int, int, int] = (3, 12, 3)

    def os_description(self) -> str:
        return self.os_text

    def kernel_release(self) -> str:
        return self.kernel

    def cpu_count(self) -> int:
        return self.cpus

    def memory_total_bytes(self) -> int | None:
        return self.memory_bytes

    def path_exists(self, path: str) -> bool:
        return path in self.existing_paths

    def path_writable(self, path: str) -> bool:
        return self.writable

    def disk_free_bytes(self, path: str) -> int | None:
        return self.free_bytes

    def which(self, executable: str) -> str | None:
        return self.executables.get(executable)

    def run(self, argv: Sequence[str], timeout_s: float = 10.0) -> CommandResult | None:
        argv_t = tuple(argv)
        for prefix, result in self.commands.items():
            if argv_t[: len(prefix)] == prefix:
                return result
        return None

    def port_free(self, port: int) -> bool:
        return port not in self.busy_ports

    def python_version(self) -> tuple[int, int, int]:
        return self.python
