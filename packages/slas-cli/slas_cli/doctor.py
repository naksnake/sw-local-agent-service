"""`slas doctor`: the preflight install.sh runs first (CLAUDE.md §3).

Each check produces one sentence. A blocking failure carries a three-part error; a warning
says what will be missing so the operator knows what will happen before it happens (§9).
Thresholds are stated in the report so the operator can see what was compared.
"""

from __future__ import annotations

from dataclasses import dataclass

from slas_cli.probe import HostProbe
from slas_cli.report import CheckResult, Report, ThreePartError

GIB = 1024**3


@dataclass(frozen=True)
class Thresholds:
    """Starting points; the platform's real sizing is CLAUDE.md §15 open decision (1)."""

    min_cpus: int = 8
    min_memory_gib: int = 32
    min_disk_gib: int = 500
    edge_port: int = 443


def _gib(n_bytes: int) -> str:
    return f"{n_bytes / GIB:.0f} GiB"


def check_operating_system(probe: HostProbe) -> CheckResult:
    return CheckResult(
        id="operating_system",
        title="Operating system",
        status="ok",
        sentence=f"{probe.os_description()}, kernel {probe.kernel_release()}.",
    )


def check_cpu_memory(probe: HostProbe, t: Thresholds) -> CheckResult:
    cpus = probe.cpu_count()
    mem = probe.memory_total_bytes()
    mem_text = _gib(mem) if mem is not None else "an unknown amount of"
    short: list[str] = []
    if cpus < t.min_cpus:
        short.append(f"{t.min_cpus} CPUs are recommended")
    if mem is not None and mem < t.min_memory_gib * GIB:
        short.append(f"{t.min_memory_gib} GiB of memory is recommended")
    if short:
        return CheckResult(
            id="cpu_memory",
            title="CPU and memory",
            status="warning",
            sentence=(
                f"{cpus} CPUs and {mem_text} memory. {' and '.join(short)}; "
                "runs will be slower and fewer sandboxes will fit at once."
            ),
        )
    return CheckResult(
        id="cpu_memory",
        title="CPU and memory",
        status="ok",
        sentence=f"{cpus} CPUs and {mem_text} memory.",
    )


def check_data_root(probe: HostProbe, data_root: str, t: Thresholds) -> CheckResult:
    exists = probe.path_exists(data_root)
    if not probe.path_writable(data_root):
        return CheckResult(
            id="data_root",
            title="Data root",
            status="blocked",
            sentence=f"{data_root} cannot be written.",
            error=ThreePartError(
                what_happened=f"The data root {data_root} is not writable by this user.",
                likely_cause="The directory or its parent belongs to another user, or the disk "
                "is mounted read-only.",
                what_to_do=f"Create {data_root} owned by the user that runs install.sh, or pass "
                "--data-root with a writable location.",
            ),
        )
    free = probe.disk_free_bytes(data_root)
    where = "exists" if exists else "will be created"
    if free is None:
        return CheckResult(
            id="data_root",
            title="Data root",
            status="warning",
            sentence=f"{data_root} {where}; free space could not be measured.",
        )
    if free < t.min_disk_gib * GIB:
        return CheckResult(
            id="data_root",
            title="Data root",
            status="warning",
            sentence=(
                f"{data_root} {where} with {_gib(free)} free; {t.min_disk_gib} GiB is "
                "recommended because model weights, run logs and screenshots live here. "
                "Installation will proceed, and the platform will pause new runs when the "
                "disk fills."
            ),
        )
    return CheckResult(
        id="data_root",
        title="Data root",
        status="ok",
        sentence=f"{data_root} {where} with {_gib(free)} free.",
    )


def _version_line(probe: HostProbe, argv: list[str]) -> str | None:
    result = probe.run(argv)
    if result is None or result.returncode != 0:
        return None
    first = result.stdout.strip().splitlines()
    return first[0].strip() if first else ""


def check_container_engine(probe: HostProbe) -> tuple[CheckResult, str | None]:
    """Returns the check and the engine name ('podman' or 'docker') when one answers."""
    for engine in ("podman", "docker"):
        if probe.which(engine) is None:
            continue
        version = _version_line(probe, [engine, "--version"])
        if version is None:
            return (
                CheckResult(
                    id="container_engine",
                    title="Container engine",
                    status="blocked",
                    sentence=f"{engine} was found but did not answer.",
                    error=ThreePartError(
                        what_happened=f"`{engine} --version` failed or timed out.",
                        likely_cause=f"The {engine} service is not running, or this user may "
                        "not use it.",
                        what_to_do=f"Start {engine} and confirm `{engine} --version` prints a "
                        "version as this user, then run ./install.sh again.",
                    ),
                ),
                None,
            )
        return (
            CheckResult(
                id="container_engine",
                title="Container engine",
                status="ok",
                sentence=f"{version}.",
            ),
            engine,
        )
    return (
        CheckResult(
            id="container_engine",
            title="Container engine",
            status="blocked",
            sentence="No container engine was found.",
            error=ThreePartError(
                what_happened="Neither podman nor docker is installed on this host.",
                likely_cause="This is a fresh host; the platform runs every service as a "
                "container and cannot start without an engine.",
                what_to_do="Install Podman (preferred, CLAUDE.md §3) or Docker from the offline "
                "bundle's OS packages, then run ./install.sh again.",
            ),
        ),
        None,
    )


def check_compose(probe: HostProbe, engine: str | None) -> CheckResult:
    if engine is None:
        return CheckResult(
            id="compose",
            title="Compose",
            status="blocked",
            sentence="Skipped because no container engine answered.",
            error=ThreePartError(
                what_happened="Compose could not be checked.",
                likely_cause="The container engine check above failed first.",
                what_to_do="Fix the container engine, then run ./install.sh again.",
            ),
        )
    version = _version_line(probe, [engine, "compose", "version"])
    if version is None and engine == "podman" and probe.which("podman-compose") is not None:
        version = _version_line(probe, ["podman-compose", "--version"])
    if version is None:
        return CheckResult(
            id="compose",
            title="Compose",
            status="blocked",
            sentence=f"`{engine} compose` is not available.",
            error=ThreePartError(
                what_happened=f"`{engine} compose version` failed.",
                likely_cause="The compose plugin is not installed alongside the engine.",
                what_to_do=f"Install the compose plugin for {engine} from the offline bundle's OS "
                "packages, then run ./install.sh again.",
            ),
        )
    return CheckResult(id="compose", title="Compose", status="ok", sentence=f"{version}.")


def check_gpu(probe: HostProbe) -> CheckResult:
    if probe.which("nvidia-smi") is None:
        return CheckResult(
            id="gpu",
            title="GPU",
            status="warning",
            sentence=(
                "No NVIDIA driver was found. Inference will not be available on this host; "
                "every other service will run, and the Models page will say so."
            ),
        )
    result = probe.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return CheckResult(
            id="gpu",
            title="GPU",
            status="warning",
            sentence=(
                "nvidia-smi is installed but reported no GPU. Inference will not be available "
                "until the driver sees a GPU; every other service will run."
            ),
        )
    rows = [line.split(",") for line in result.stdout.strip().splitlines() if line.strip()]
    names = sorted({row[0].strip() for row in rows})
    driver = rows[0][2].strip() if len(rows[0]) > 2 else "unknown"
    total_mib = 0
    for row in rows:
        try:
            total_mib += int(float(row[1].strip()))
        except (ValueError, IndexError):
            pass
    plural = "GPU" if len(rows) == 1 else "GPUs"
    return CheckResult(
        id="gpu",
        title="GPU",
        status="ok",
        sentence=(
            f"{len(rows)} {plural} ({', '.join(names)}), {total_mib // 1024} GiB of GPU memory "
            f"in total, driver {driver}."
        ),
    )


def check_gvisor(probe: HostProbe) -> CheckResult:
    if probe.which("runsc") is None:
        return CheckResult(
            id="gvisor",
            title="Sandbox runtime",
            status="warning",
            sentence=(
                "gVisor (runsc) was not found. Code sandboxes will fall back to hardened runc, "
                "and every coding ticket will say so."
            ),
        )
    return CheckResult(
        id="gvisor",
        title="Sandbox runtime",
        status="ok",
        sentence="gVisor (runsc) is available for code sandboxes.",
    )


def check_edge_port(probe: HostProbe, t: Thresholds) -> CheckResult:
    if probe.port_free(t.edge_port):
        return CheckResult(
            id="edge_port",
            title="Web port",
            status="ok",
            sentence=f"Port {t.edge_port} is free for the web interface.",
        )
    return CheckResult(
        id="edge_port",
        title="Web port",
        status="blocked",
        sentence=f"Port {t.edge_port} is in use.",
        error=ThreePartError(
            what_happened=f"Another program is already listening on port {t.edge_port}.",
            likely_cause="A web server or a previous installation is running on this host.",
            what_to_do=f"Stop the program using port {t.edge_port}, or set SLAS_EDGE_PORT to a "
            "free port in .env after installation.",
        ),
    )


def check_python(probe: HostProbe) -> CheckResult:
    major, minor, micro = probe.python_version()
    return CheckResult(
        id="python",
        title="Python",
        status="ok",
        sentence=f"Python {major}.{minor}.{micro} runs this preflight.",
    )


def run_doctor(
    probe: HostProbe,
    *,
    data_root: str,
    profile: str = "quickstart",
    thresholds: Thresholds | None = None,
    product: str = "SW Local Agent Service",
) -> Report:
    t = thresholds or Thresholds()
    engine_check, engine = check_container_engine(probe)
    checks = [
        check_operating_system(probe),
        check_cpu_memory(probe, t),
        check_data_root(probe, data_root, t),
        engine_check,
        check_compose(probe, engine),
        check_gpu(probe),
        check_gvisor(probe),
        check_edge_port(probe, t),
        check_python(probe),
    ]
    return Report(product=product, data_root=data_root, profile=profile, checks=checks)
