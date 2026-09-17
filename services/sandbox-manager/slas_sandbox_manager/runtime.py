"""The sandbox runtime boundary: the protocol, its fake, and the Engine API driver (ADR-0015).

`ContainerApiRuntime` translates a `SandboxSpec` into a `slas_container.CreateSpec` that
carries exactly the hardening `spec.podman_argv` encodes — no network, read-only rootfs, a
private tmpfs, every capability dropped, no new privileges, pids/memory/CPU caps, uid 10001,
`/workspace`, gVisor or hardened runc — and drives it over the runtime socket with
`slas_container.ContainerApi`. The manager sees the data root at `/data`; the runtime sees
it at `${SLAS_HOST_DATA_ROOT}`, so every mount source is translated by prefix before it
reaches the socket. Commands are argv only, as the workspace user.

`detect_isolation()` answers the start-up question "is gVisor here?" by starting and
removing a throwaway container under `runsc`; when it is not, the quickstart profile falls
back to hardened runc and says so in one sentence (CLAUDE.md §3).
"""

from __future__ import annotations

import contextlib
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from pydantic import Field

from slas_container import ContainerError, ContainerInfo, CreateSpec, EngineInfo
from slas_container import ExecResult as ContainerExecResult
from slas_container import Mount as ContainerMount
from slas_container.spec import Engine
from slas_sandbox_manager.spec import (
    SANDBOX_UID,
    SECCOMP_PROFILE,
    WORKSPACE,
    Runtime,
    SandboxSpec,
    check_hardening,
)
from slas_schemas.common import SlasModel

#: Every sandbox container carries this label; the manager only ever touches those.
SANDBOX_LABEL_KEY: Final = "slas.kind"
SANDBOX_LABEL_VALUE: Final = "sandbox"
#: What keeps the container idle until `exec` gives it work; every image ships GNU sleep.
IDLE_ARGV: Final[tuple[str, ...]] = ("sleep", "infinity")
_SIZE = re.compile(r"^(\d+)([kmg])$")
_UNITS: Final[dict[str, int]] = {"k": 1024, "m": 1024**2, "g": 1024**3}


class ExecResult(SlasModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxHandle(SlasModel):
    id: str = Field(min_length=1)
    spec: SandboxSpec

    @property
    def name(self) -> str:
        return self.spec.name


class SandboxRuntime(Protocol):
    def create(self, spec: SandboxSpec) -> SandboxHandle: ...

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult: ...

    def destroy(self, handle: SandboxHandle) -> None: ...

    def alive(self, handle: SandboxHandle) -> bool: ...


ExecHandler = Callable[[Sequence[str], str], ExecResult | None]


class FakeSandboxRuntime:
    """Sandboxes as records; commands answered by scripted results or a handler."""

    def __init__(self) -> None:
        self._alive: dict[str, SandboxHandle] = {}
        self.created: list[SandboxSpec] = []
        self.destroyed: list[str] = []
        self.execs: list[tuple[str, tuple[str, ...], str]] = []
        self._scripts: dict[tuple[str, ...], list[ExecResult]] = {}
        self._handler: ExecHandler | None = None
        self._counter = 0

    def script(self, argv: Sequence[str], *results: ExecResult) -> None:
        """Answer `argv` with the given results in order; the last one repeats."""
        self._scripts.setdefault(tuple(argv), []).extend(results)

    def handle_with(self, handler: ExecHandler) -> None:
        self._handler = handler

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        if spec.name in self._alive:
            raise RuntimeError(f"a sandbox named {spec.name} is already running")
        self._counter += 1
        handle = SandboxHandle(id=f"sbx{self._counter:03d}", spec=spec)
        self._alive[spec.name] = handle
        self.created.append(spec)
        return handle

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        if handle.name not in self._alive:
            raise RuntimeError(f"sandbox {handle.name} is not running")
        self.execs.append((handle.name, tuple(argv), cwd))
        if self._handler is not None:
            answer = self._handler(argv, cwd)
            if answer is not None:
                return answer
        queue = self._scripts.get(tuple(argv))
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return ExecResult(exit_code=0, stdout="")

    def destroy(self, handle: SandboxHandle) -> None:
        self._alive.pop(handle.name, None)
        self.destroyed.append(handle.name)

    def alive(self, handle: SandboxHandle) -> bool:
        return handle.name in self._alive


# --- the Engine API driver ----------------------------------------------------------------------


class ContainerApiLike(Protocol):
    """The part of `slas_container.ContainerApi` this driver uses; the fake has it too."""

    @property
    def engine(self) -> Engine: ...

    def ping(self) -> EngineInfo: ...

    def create(self, spec: CreateSpec) -> str: ...

    def start(self, name: str) -> None: ...

    def stop(self, name: str, *, timeout_s: int = 30) -> None: ...

    def remove(self, name: str, *, force: bool = True) -> None: ...

    def inspect(self, name: str) -> ContainerInfo | None: ...

    def image_present(self, reference: str) -> bool: ...

    def exec(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float = 600.0,
    ) -> ContainerExecResult: ...


def parse_size(text: str) -> int:
    """`4g` → bytes; the units `Resources` allows (k, m, g)."""
    match = _SIZE.match(text.strip().lower())
    if match is None:
        raise ValueError(f"{text!r} is not a size such as 512m or 4g")
    return int(match.group(1)) * _UNITS[match.group(2)]


def translate_path(path: str, *, container_root: str, host_root: str) -> str:
    """The host side of a path the manager sees under its own data root; others pass through."""
    inside = Path(path)
    root = Path(container_root)
    if inside == root:
        return str(Path(host_root))
    try:
        relative = inside.relative_to(root)
    except ValueError:
        return path
    return str(Path(host_root) / relative)


def seccomp_option(engine: Engine, profile: Path | None) -> str | None:
    """`seccomp=…` for hardened runc, in the form the engine's API takes.

    Docker's CLI reads the profile file and sends its JSON inline (the daemon never opens a
    path); Podman's compat API takes a path on its own host. Without a readable profile the
    engine's default seccomp profile applies and the runtime sentence says so.
    """
    if profile is None or not profile.is_file():
        return None
    if engine == "docker":
        return f"seccomp={profile.read_text(encoding='utf-8').strip()}"
    return f"seccomp={profile}"


class ContainerApiRuntime:
    """`SandboxRuntime` over the runtime socket."""

    def __init__(
        self,
        api: ContainerApiLike,
        *,
        host_data_root: str | Path,
        container_data_root: str | Path = "/data",
        seccomp_profile: Path | None = Path(SECCOMP_PROFILE),
        stop_timeout_s: int = 5,
    ) -> None:
        self.api = api
        self.host_data_root = str(host_data_root)
        self.container_data_root = str(container_data_root)
        self.seccomp_profile = seccomp_profile
        self.stop_timeout_s = stop_timeout_s

    # --- translation --------------------------------------------------------------------

    def host_path(self, path: str) -> str:
        return translate_path(
            path, container_root=self.container_data_root, host_root=self.host_data_root
        )

    def create_spec(self, spec: SandboxSpec) -> CreateSpec:
        """The Engine API body for this sandbox, with the same hardening as `podman_argv`."""
        check_hardening(spec)
        security_opt: list[str] = []
        if spec.runtime == "runc":
            option = seccomp_option(self.api.engine, self.seccomp_profile)
            if option is not None:
                security_opt.append(option)
        return CreateSpec(
            name=spec.name,
            image=spec.image,
            argv=list(IDLE_ARGV),
            env=dict(spec.env),
            network="none",
            mounts=[
                ContainerMount(
                    source=self.host_path(mount.source),
                    target=mount.target,
                    read_only=mount.mode == "ro",
                )
                for mount in spec.mounts
            ],
            tmpfs={"/tmp": f"rw,nosuid,nodev,noexec,size={spec.resources.tmp_size}"},  # noqa: S108 — the container's private tmpfs
            runtime=spec.runtime,
            user=f"{SANDBOX_UID}:{SANDBOX_UID}",
            workdir=WORKSPACE,
            read_only_rootfs=True,
            cap_drop_all=True,
            no_new_privileges=True,
            security_opt=security_opt,
            pids_limit=spec.resources.pids,
            memory_bytes=parse_size(spec.resources.memory),
            nano_cpus=round(spec.resources.cpus * 1_000_000_000),
            labels={
                SANDBOX_LABEL_KEY: SANDBOX_LABEL_VALUE,
                "slas.user": spec.user,
                "slas.project": spec.slug,
                "slas.ttl_s": str(spec.ttl_s),
            },
            restart="no",
            stop_timeout_s=self.stop_timeout_s,
        )

    def seccomp_sentence(self) -> str:
        if self.seccomp_profile is None or not self.seccomp_profile.is_file():
            return (
                "No seccomp profile is installed at "
                f"{self.seccomp_profile or SECCOMP_PROFILE}; hardened runc sandboxes run under "
                "the engine's default profile."
            )
        return f"Hardened runc sandboxes run under the seccomp profile {self.seccomp_profile}."

    # --- SandboxRuntime -----------------------------------------------------------------

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        body = self.create_spec(spec)
        try:
            container_id = self.api.create(body)
            self.api.start(spec.name)
        except ContainerError:
            # A container that was created but never started must not linger.
            with contextlib.suppress(ContainerError):
                self.api.remove(spec.name, force=True)
            raise
        return SandboxHandle(id=container_id or spec.name, spec=spec)

    def exec(
        self,
        handle: SandboxHandle,
        argv: Sequence[str],
        *,
        cwd: str = WORKSPACE,
        timeout_s: int = 600,
        stdin: str | None = None,
    ) -> ExecResult:
        if not argv or any(not isinstance(part, str) for part in argv):
            raise ValueError("argv must be a non-empty list of strings")
        if stdin is not None:
            raise ValueError(
                "stdin is not carried over the runtime socket; write the input into the "
                "workspace and pass its path"
            )
        result = self.api.exec(
            handle.name,
            list(argv),
            cwd=cwd,
            user=f"{SANDBOX_UID}:{SANDBOX_UID}",
            timeout_s=float(timeout_s),
        )
        return ExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )

    def destroy(self, handle: SandboxHandle) -> None:
        self.api.stop(handle.name, timeout_s=self.stop_timeout_s)
        self.api.remove(handle.name, force=True)

    def alive(self, handle: SandboxHandle) -> bool:
        info = self.api.inspect(handle.name)
        return info is not None and info.running


# --- runtime detection --------------------------------------------------------------------------


@dataclass(frozen=True)
class Isolation:
    """What the start-up probe found; the manager and `/health` read it."""

    engine: EngineInfo
    runsc_available: bool
    kata_available: bool
    sentence: str

    @property
    def isolation(self) -> str:
        return "gvisor" if self.runsc_available else "runc"


def probe_runtime(
    api: ContainerApiLike, runtime: Runtime, images: Sequence[str]
) -> tuple[bool | None, str]:
    """Start and remove a throwaway container under `runtime`.

    Returns (available, detail): `None` when no sandbox image is present to probe with, so
    the caller keeps its configured default and says so.
    """
    image = next((candidate for candidate in images if api.image_present(candidate)), None)
    if image is None:
        return None, "no sandbox image is loaded yet to probe with"
    name = f"slas-sbx-probe-{secrets.token_hex(4)}"
    spec = CreateSpec(
        name=name,
        image=image,
        argv=["true"],
        network="none",
        runtime=runtime,
        user=f"{SANDBOX_UID}:{SANDBOX_UID}",
        read_only_rootfs=True,
        labels={SANDBOX_LABEL_KEY: "probe"},
        stop_timeout_s=1,
    )
    try:
        api.create(spec)
        api.start(name)
    except ContainerError as exc:
        return False, exc.message.likely_cause
    finally:
        with contextlib.suppress(ContainerError):
            api.remove(name, force=True)
    return True, f"a throwaway container started under {runtime}"


def detect_isolation(
    api: ContainerApiLike,
    *,
    default_runtime: Runtime = "runsc",
    tier: str = "gvisor",
    probe_images: Sequence[str] = (),
) -> Isolation:
    """Ping the socket, then find out whether gVisor (and, for the kata tier, kata-fc) is there.

    Raises `ContainerError` when the socket does not answer; the service then reports
    `runtime: down` and retries on the next probe.
    """
    engine = api.ping()
    kata_available = False
    kata_detail = ""
    if tier == "kata":
        kata_found, kata_detail = probe_runtime(api, "kata-fc", probe_images)
        kata_available = bool(kata_found)
    if default_runtime != "runsc":
        found: bool | None = False
        detail = f"DEFAULT_RUNTIME is {default_runtime}, so gVisor was not probed"
    else:
        found, detail = probe_runtime(api, "runsc", probe_images)
    if found is None:
        # Keep the configured default; the first sandbox to open reports the truth.
        runsc_available = default_runtime == "runsc"
        sentence = (
            f"{engine.sentence()} Whether gVisor (runsc) is registered could not be checked "
            f"yet ({detail}); sandboxes will ask for {default_runtime} as configured."
        )
    elif found:
        runsc_available = True
        sentence = f"{engine.sentence()} gVisor (runsc) is registered; sandboxes run under it."
    else:
        runsc_available = False
        sentence = (
            f"{engine.sentence()} gVisor (runsc) is not registered on this host ({detail}), "
            "so sandboxes fall back to hardened runc: no network, read-only system, every "
            "capability dropped, a seccomp profile where one is installed. Install gVisor and "
            "register runsc with the runtime to lift this."
        )
    if tier == "kata":
        sentence += (
            " The Kata/Firecracker tier is registered."
            if kata_available
            else f" The Kata/Firecracker tier (kata-fc) is not registered ({kata_detail})."
        )
    return Isolation(
        engine=engine,
        runsc_available=runsc_available,
        kata_available=kata_available,
        sentence=sentence,
    )
