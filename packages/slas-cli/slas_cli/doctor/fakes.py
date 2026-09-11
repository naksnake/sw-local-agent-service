"""A scripted host for tests. Nothing here touches the real machine.

`FakeHost.healthy()` describes a host that passes every check; tests break one thing at a
time and assert on the resulting sentence. Every `run()` call is recorded in `calls` so a
test can assert which commands a check asked for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from slas_cli.doctor.host import CommandResult

GIB = 1024**3


@dataclass
class FakeHost:
    system_name: str = "Linux"
    release: str = "6.8.0-45-generic"
    machine_name: str = "x86_64"
    cpus: int | None = 64
    memory_bytes: int | None = 512 * GIB
    commands: dict[str, str] = field(default_factory=dict)
    outputs: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    existing_paths: set[str] = field(default_factory=set)
    directories: set[str] = field(default_factory=set)
    writable_paths: set[str] = field(default_factory=set)
    disk_free: dict[str, int] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    ports: dict[int, bool | None] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    @classmethod
    def healthy(cls, data_root: str = "/AI/Agent") -> FakeHost:
        """A GPU host with Docker, rootless Podman, gVisor and plenty of room."""
        host = cls()
        for name in (
            "docker",
            "podman",
            "runsc",
            "nvidia-smi",
            "nvidia-ctk",
            "newuidmap",
            "newgidmap",
        ):
            host.commands[name] = f"/usr/bin/{name}"
        host.outputs[("docker", "--version")] = CommandResult(
            0, "Docker version 27.3.1, build ce12230\n"
        )
        host.outputs[("docker", "compose", "version", "--short")] = CommandResult(0, "2.29.7\n")
        host.outputs[("docker", "info", "--format", "{{.ServerVersion}}")] = CommandResult(
            0, "27.3.1\n"
        )
        host.outputs[("podman", "--version")] = CommandResult(0, "podman version 4.9.3\n")
        host.outputs[
            ("nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits")
        ] = CommandResult(0, "NVIDIA H100 80GB HBM3, 81559\nNVIDIA H100 80GB HBM3, 81559\n")
        host.outputs[("nvidia-ctk", "--version")] = CommandResult(
            0, "NVIDIA Container Toolkit CLI version 1.16.2\ncommit: 0123456\n"
        )
        host.existing_paths.update({"/", "/AI", data_root})
        host.directories.update({"/", "/AI", data_root})
        host.writable_paths.update({"/AI", data_root})
        host.disk_free[data_root] = 2000 * GIB
        host.files["/proc/sys/user/max_user_namespaces"] = "28633\n"
        host.files["/sys/fs/cgroup/cgroup.controllers"] = "cpuset cpu io memory pids\n"
        host.ports[443] = False
        return host

    # --- Host protocol -------------------------------------------------------------

    def system(self) -> str:
        return self.system_name

    def kernel_release(self) -> str:
        return self.release

    def machine(self) -> str:
        return self.machine_name

    def cpu_count(self) -> int | None:
        return self.cpus

    def memory_total_bytes(self) -> int | None:
        return self.memory_bytes

    def which(self, command: str) -> str | None:
        return self.commands.get(command)

    def run(self, argv: Sequence[str], timeout_s: float = 10.0) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        if not argv or argv[0] not in self.commands:
            return CommandResult(None, "")
        if key in self.outputs:
            return self.outputs[key]
        if (argv[0],) in self.outputs:
            return self.outputs[(argv[0],)]
        return CommandResult(0, "")

    def path_exists(self, path: str) -> bool:
        return path in self.existing_paths or path in self.files

    def is_dir(self, path: str) -> bool:
        return path in self.directories

    def is_writable(self, path: str) -> bool:
        return path in self.writable_paths

    def disk_free_bytes(self, path: str) -> int | None:
        return self.disk_free.get(path)

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)

    def port_in_use(self, port: int) -> bool | None:
        return self.ports.get(port)

    def env(self, name: str) -> str | None:
        return self.environment.get(name)
