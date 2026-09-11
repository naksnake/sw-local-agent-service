"""The preflight checks behind `slas doctor` and `./install.sh`.

Each check is a pure function of a `Host`: it asks questions, changes nothing, and returns
one `CheckResult` whose `summary` is a sentence an engineer can read. Anything that is not
"ok" also carries a three-part message — what happened, likely cause, what to do
(CLAUDE.md §11).

Thresholds are assumptions until the model bundle size and the GPU budget are fixed
(CLAUDE.md §15, open decision 2). Each lives in `DoctorSettings` so the caller can override.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from slas_cli.doctor.host import Host
from slas_kernel.branding import DEFAULT_DATA_ROOT
from slas_schemas.errors import ThreePartMessage

Status = Literal["ok", "warn", "fail", "skip"]

GIB = 1024**3
MIB = 1024**2

_VERSION_AFTER_WORD = re.compile(r"version\s+v?([0-9][\w.\-]*)")


@dataclass(frozen=True, slots=True)
class DoctorSettings:
    """Everything a check may need besides the host itself."""

    data_root: str = DEFAULT_DATA_ROOT
    profile: str = "quickstart"
    # Assumption: placeholders until the model bundle and GPU budget are fixed (§15).
    min_cpu_cores: int = 8
    min_memory_gib: int = 32
    min_free_disk_gib: int = 200
    web_port: int = 443


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One line of the report. `summary` is always a sentence."""

    check_id: str
    title: str
    status: Status
    summary: str
    detail: ThreePartMessage | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "id": self.check_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "what_happened": self.detail.what_happened if self.detail else None,
            "likely_cause": self.detail.likely_cause if self.detail else None,
            "what_to_do": self.detail.what_to_do if self.detail else None,
        }


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What the report header says about the machine."""

    system: str
    kernel_release: str
    machine: str
    cpu_cores: int | None
    memory_gib: float | None

    def sentence(self, separator: str = " · ") -> str:
        parts = [f"{self.system or 'Unknown OS'} {self.kernel_release} on {self.machine}".strip()]
        parts.append(
            f"{self.cpu_cores} cores" if self.cpu_cores is not None else "core count unknown"
        )
        parts.append(
            f"{_fmt_gib(self.memory_gib)} memory"
            if self.memory_gib is not None
            else "memory size unknown"
        )
        return separator.join(parts)


Check = Callable[[Host, DoctorSettings], CheckResult]


# --- helpers ---------------------------------------------------------------------------


def _ok(check_id: str, title: str, summary: str) -> CheckResult:
    return CheckResult(check_id, title, "ok", summary)


def _skip(check_id: str, title: str, summary: str) -> CheckResult:
    return CheckResult(check_id, title, "skip", summary)


def _problem(
    check_id: str,
    title: str,
    status: Literal["warn", "fail"],
    what_happened: str,
    likely_cause: str,
    what_to_do: str,
) -> CheckResult:
    detail = ThreePartMessage(what_happened, likely_cause, what_to_do)
    return CheckResult(check_id, title, status, what_happened, detail)


def _fmt_gib(gib: float) -> str:
    return f"{gib:.0f} GiB" if gib >= 10 else f"{gib:.1f} GiB"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count} {word}"


def _version_from(text: str) -> str | None:
    match = _VERSION_AFTER_WORD.search(text)
    return match.group(1) if match else None


def _nearest_existing(host: Host, path: str) -> str | None:
    """The path itself if it exists, else its closest existing ancestor."""
    current = PurePosixPath(path)
    for candidate in (current, *current.parents):
        if host.path_exists(str(candidate)):
            return str(candidate)
    return None


def _parse_gpus(text: str) -> list[tuple[str, int]]:
    """Parse `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits`."""
    gpus: list[tuple[str, int]] = []
    for line in text.splitlines():
        name, sep, memory = line.rpartition(",")
        if not sep:
            continue
        try:
            gpus.append((name.strip(), int(memory.strip())))
        except ValueError:
            continue
    return gpus


def describe_host(host: Host) -> HostFacts:
    memory = host.memory_total_bytes()
    return HostFacts(
        system=host.system(),
        kernel_release=host.kernel_release(),
        machine=host.machine(),
        cpu_cores=host.cpu_count(),
        memory_gib=memory / GIB if memory is not None else None,
    )


# --- checks ----------------------------------------------------------------------------


def check_operating_system(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "operating_system", "Operating system"
    system = host.system()
    if system == "Linux":
        return _ok(check_id, title, f"Linux {host.kernel_release()} on {host.machine()}.")
    shown = system or "an operating system that could not be identified"
    return _problem(
        check_id,
        title,
        "fail",
        f"This host runs {shown}, not Linux.",
        "The platform needs a Linux host for containers, GPU access and rootless sandboxes.",
        "Install on a Linux server with an NVIDIA GPU.",
    )


def check_cpu(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "cpu", "Processor"
    cores = host.cpu_count()
    if cores is None:
        return _skip(check_id, title, "Could not count the processor cores.")
    if cores >= settings.min_cpu_cores:
        return _ok(
            check_id,
            title,
            f"{_plural(cores, 'processor core')} (at least {settings.min_cpu_cores} recommended).",
        )
    return _problem(
        check_id,
        title,
        "warn",
        f"Only {_plural(cores, 'processor core')} were found; "
        f"at least {settings.min_cpu_cores} are recommended.",
        "The platform runs several services, model instances and sandboxes side by side.",
        f"It will run, but slowly. Use a host with at least {settings.min_cpu_cores} cores "
        "for daily use.",
    )


def check_memory(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "memory", "Memory"
    total = host.memory_total_bytes()
    if total is None:
        return _skip(check_id, title, "Could not read the amount of memory.")
    gib = total / GIB
    if gib >= settings.min_memory_gib:
        return _ok(
            check_id,
            title,
            f"{_fmt_gib(gib)} of memory (at least {settings.min_memory_gib} GiB recommended).",
        )
    return _problem(
        check_id,
        title,
        "warn",
        f"Only {_fmt_gib(gib)} of memory was found; "
        f"at least {settings.min_memory_gib} GiB is recommended.",
        "Models load into GPU memory, but the services, the knowledge index and the sandboxes "
        "need host memory.",
        f"It will run with fewer sandboxes at once. Add memory to reach at least "
        f"{settings.min_memory_gib} GiB.",
    )


def check_container_runtime(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "container_runtime", "Container runtime"
    if host.which("docker") is None:
        return _problem(
            check_id,
            title,
            "fail",
            "Docker was not found.",
            "The platform services start with docker compose, which is not installed or not "
            "on PATH.",
            "Install Docker Engine with the Compose plugin, add your user to the docker group, "
            "log in again and run ./install.sh again.",
        )
    docker = host.run(["docker", "--version"])
    docker_version = _version_from(docker.stdout) if docker.ok else None
    compose = host.run(["docker", "compose", "version", "--short"])
    if not compose.ok or not compose.stdout.strip():
        return _problem(
            check_id,
            title,
            "fail",
            "Docker is installed, but the Compose plugin is not.",
            "The platform starts its services with `docker compose`, which is a separate plugin.",
            "Install the docker-compose-plugin package for your distribution and run "
            "./install.sh again.",
        )
    daemon = host.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout_s=20.0)
    if not daemon.ok:
        return _problem(
            check_id,
            title,
            "fail",
            "Docker is installed, but the Docker service did not answer.",
            "The Docker daemon is stopped, or your user is not allowed to use it.",
            "Start Docker (sudo systemctl enable --now docker), make sure your user is in the "
            "docker group, log in again and run ./install.sh again.",
        )
    compose_version = compose.stdout.strip().lstrip("v")
    docker_shown = docker_version or "of an unknown version"
    return _ok(
        check_id,
        title,
        f"Docker {docker_shown} with Compose {compose_version} is ready.",
    )


def check_sandbox_runtime(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "sandbox_runtime", "Sandbox runtime"
    if host.which("podman") is None:
        return _problem(
            check_id,
            title,
            "warn",
            "Rootless Podman was not found; Coding Agent sandboxes need it.",
            "Coding Agent sandboxes run under rootless Podman, which is not installed yet.",
            "You can install the platform now and add Podman before using the Coding Agent "
            "(Phase 6). On Debian or Ubuntu: sudo apt install podman uidmap.",
        )
    result = host.run(["podman", "--version"])
    version = _version_from(result.stdout) if result.ok else None
    shown = f"Podman {version}" if version else "Podman"
    return _ok(check_id, title, f"{shown} is installed for the sandboxes.")


def check_sandbox_isolation(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "sandbox_isolation", "Sandbox isolation"
    if host.which("runsc") is not None:
        return _ok(check_id, title, "gVisor (runsc) is installed; sandboxes will use it.")
    if settings.profile == "prod":
        return _problem(
            check_id,
            title,
            "fail",
            "gVisor (runsc) was not found, and the prod profile requires it.",
            "The prod profile isolates model-authored code with gVisor and does not fall back.",
            "Install gVisor (runsc) and register it as a container runtime, then run "
            "./install.sh again.",
        )
    return _problem(
        check_id,
        title,
        "warn",
        "gVisor (runsc) was not found; sandboxes will use hardened runc instead.",
        "Quickstart uses gVisor when it is installed and falls back to hardened runc otherwise.",
        "For stronger isolation, install gVisor (runsc) before using the Coding Agent. "
        "The prod profile requires it.",
    )


def check_user_namespaces(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "user_namespaces", "User namespaces"
    text = host.read_text("/proc/sys/user/max_user_namespaces")
    if text is None:
        return _skip(check_id, title, "Could not read the user-namespace limit.")
    try:
        limit = int(text.strip())
    except ValueError:
        return _skip(check_id, title, "The user-namespace limit could not be understood.")
    if limit > 0:
        return _ok(check_id, title, f"Unprivileged user namespaces are enabled (limit {limit}).")
    return _problem(
        check_id,
        title,
        "warn",
        "Unprivileged user namespaces are disabled.",
        "Rootless Podman and gVisor use user namespaces to isolate sandboxes.",
        "Set user.max_user_namespaces to a value above 0 (for example 28633) with sysctl, "
        "then run ./install.sh again.",
    )


def check_id_mapping_helpers(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "id_mapping_helpers", "Rootless helpers"
    missing = [name for name in ("newuidmap", "newgidmap") if host.which(name) is None]
    if not missing:
        return _ok(check_id, title, "newuidmap and newgidmap are installed.")
    return _problem(
        check_id,
        title,
        "warn",
        f"{' and '.join(missing)} {'was' if len(missing) == 1 else 'were'} not found.",
        "Rootless containers map user ids with these helpers from the uidmap package.",
        "Install uidmap (Debian/Ubuntu) or shadow-utils (RHEL) before using the Coding Agent.",
    )


def check_cgroups_v2(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "cgroups_v2", "Control groups"
    if host.path_exists("/sys/fs/cgroup/cgroup.controllers"):
        return _ok(check_id, title, "cgroups v2 is in use.")
    return _problem(
        check_id,
        title,
        "warn",
        "This host is not using cgroups v2.",
        "Rootless containers need cgroups v2 to enforce CPU and memory limits.",
        "Boot with systemd.unified_cgroup_hierarchy=1 (the default on recent distributions) "
        "and run ./install.sh again.",
    )


def check_gpu(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "gpu", "GPU"
    if host.which("nvidia-smi") is None:
        return _problem(
            check_id,
            title,
            "fail",
            "No NVIDIA GPU driver was found (nvidia-smi is missing).",
            "Inference runs on local GPUs. Without the driver, no model can load.",
            "Install the NVIDIA driver for your GPU, reboot, and run ./install.sh again.",
        )
    result = host.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
        timeout_s=20.0,
    )
    gpus = _parse_gpus(result.stdout) if result.ok else []
    if not gpus:
        return _problem(
            check_id,
            title,
            "fail",
            "nvidia-smi is installed but could not list any GPU.",
            "The driver is not loaded, or the GPU is not visible to this system.",
            "Run nvidia-smi yourself to see its message, reboot if the driver was just "
            "installed, then run ./install.sh again.",
        )
    counts = Counter(name for name, _ in gpus)
    if len(counts) == 1:
        names = next(iter(counts))
    else:
        names = ", ".join(f"{count} of {name}" for name, count in counts.items())
    total_gib = sum(memory for _, memory in gpus) * MIB / GIB
    return _ok(
        check_id,
        title,
        f"{_plural(len(gpus), 'GPU')} found: {names}, "
        f"{_fmt_gib(total_gib)} of GPU memory in total.",
    )


def check_gpu_container_toolkit(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "gpu_container_toolkit", "GPU in containers"
    if host.which("nvidia-smi") is None:
        return _skip(check_id, title, "Skipped because no GPU driver was found.")
    if host.which("nvidia-ctk") is None:
        return _problem(
            check_id,
            title,
            "fail",
            "The NVIDIA Container Toolkit was not found (nvidia-ctk is missing).",
            "Containers cannot reach the GPU without it, so the model instances would start "
            "without a GPU.",
            "Install nvidia-container-toolkit, run sudo nvidia-ctk runtime configure "
            "--runtime=docker, restart Docker, then run ./install.sh again.",
        )
    result = host.run(["nvidia-ctk", "--version"])
    version = _version_from(result.stdout) if result.ok else None
    shown = f"NVIDIA Container Toolkit {version}" if version else "The NVIDIA Container Toolkit"
    return _ok(check_id, title, f"{shown} is installed.")


def check_data_root(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "data_root", "Data root"
    path = settings.data_root
    if host.path_exists(path):
        if not host.is_dir(path):
            return _problem(
                check_id,
                title,
                "fail",
                f"The data root {path} exists but is not a directory.",
                "A file or a link occupies the path where the platform keeps its data.",
                "Move it aside, or set SLAS_DATA_ROOT to a directory and run ./install.sh again.",
            )
        if host.is_writable(path):
            return _ok(check_id, title, f"{path} exists and is writable.")
        return _problem(
            check_id,
            title,
            "fail",
            f"The data root {path} exists but is not writable by you.",
            "It belongs to another user or has restrictive permissions.",
            f"Give your user write access (sudo chown -R $USER {path}) or set SLAS_DATA_ROOT "
            "to another location, then run ./install.sh again.",
        )
    ancestor = _nearest_existing(host, path)
    if ancestor is not None and host.is_writable(ancestor):
        return _ok(
            check_id, title, f"{path} does not exist yet; it will be created under {ancestor}."
        )
    where = ancestor or "its parent directory"
    return _problem(
        check_id,
        title,
        "fail",
        f"The data root {path} does not exist and cannot be created.",
        f"{where} is not writable by you.",
        f"Create it yourself (sudo mkdir -p {path} && sudo chown $USER {path}) or set "
        "SLAS_DATA_ROOT to a writable location, then run ./install.sh again.",
    )


def check_disk_space(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "disk_space", "Disk space"
    anchor = _nearest_existing(host, settings.data_root)
    free = host.disk_free_bytes(anchor) if anchor is not None else None
    if anchor is None or free is None:
        return _skip(check_id, title, f"Could not measure free space for {settings.data_root}.")
    free_gib = free / GIB
    if free_gib >= settings.min_free_disk_gib:
        return _ok(
            check_id,
            title,
            f"{_fmt_gib(free_gib)} free at {anchor} "
            f"(at least {settings.min_free_disk_gib} GiB needed).",
        )
    return _problem(
        check_id,
        title,
        "fail",
        f"Only {_fmt_gib(free_gib)} is free where the data root will live ({anchor}).",
        "Model weights, container images, run artifacts and backups live under the data root "
        f"and need at least {settings.min_free_disk_gib} GiB.",
        f"Free space, mount a larger disk at {settings.data_root}, or point SLAS_DATA_ROOT at "
        "one, then run ./install.sh again.",
    )


def check_web_port(host: Host, settings: DoctorSettings) -> CheckResult:
    check_id, title = "web_port", "Web port"
    port = settings.web_port
    in_use = host.port_in_use(port)
    if in_use is None:
        return _skip(check_id, title, f"Could not check whether port {port} is free.")
    if not in_use:
        return _ok(check_id, title, f"Port {port} is free for the web interface.")
    return _problem(
        check_id,
        title,
        "fail",
        f"Something is already listening on port {port}.",
        "Another web server or an earlier installation is using the port the web interface needs.",
        "Stop the other service, or set SLAS_HTTPS_PORT to a free port in .env, then run "
        "./install.sh again.",
    )


ALL_CHECKS: tuple[Check, ...] = (
    check_operating_system,
    check_cpu,
    check_memory,
    check_container_runtime,
    check_sandbox_runtime,
    check_sandbox_isolation,
    check_user_namespaces,
    check_id_mapping_helpers,
    check_cgroups_v2,
    check_gpu,
    check_gpu_container_toolkit,
    check_data_root,
    check_disk_space,
    check_web_port,
)


def _guarded(check: Check, host: Host, settings: DoctorSettings) -> CheckResult:
    """A bug in one check must not hide the rest of the report."""
    title = check.__name__.removeprefix("check_").replace("_", " ").capitalize()
    try:
        return check(host, settings)
    except Exception as exc:
        return _problem(
            check.__name__.removeprefix("check_"),
            title,
            "warn",
            f"The {title.lower()} check could not run ({type(exc).__name__}: {exc}).",
            "This is a bug in the preflight, not a problem with your host.",
            "Run ./install.sh again; if it repeats, report the line above.",
        )


def run_checks(
    host: Host,
    settings: DoctorSettings,
    checks: Sequence[Check] = ALL_CHECKS,
) -> list[CheckResult]:
    return [_guarded(check, host, settings) for check in checks]
