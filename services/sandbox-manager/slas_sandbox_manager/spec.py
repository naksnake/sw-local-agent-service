"""The sandbox container spec and the hardening gate (CLAUDE.md §4.1 Zone A, INV-4, INV-14).

A sandbox has no network, a read-only rootfs, every capability dropped, no new privileges,
a pids limit, memory and CPU caps, a private tmpfs, and exactly three mounts: the project
(rw), a per-session scratch overlay (rw) and the user's git identity (ro). The spec refuses
anything else at construction: a secret-looking environment variable, a runtime socket, the
host X11 socket, `/dev/input`, an SSH directory or a credential file. gVisor (`runsc`) is the
runtime; the hardened `runc` fallback adds user-namespace isolation and a seccomp profile.
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

#: gVisor (`runsc`), the Kata/Firecracker micro-VM tier (`kata-fc`, prod), hardened runc.
Runtime = Literal["runsc", "kata-fc", "runc"]
Tier = Literal["gvisor", "kata"]

WORKSPACE: Final = "/workspace"
SCRATCH: Final = "/scratch"
GITCONFIG_TARGET: Final = "/etc/slas/gitconfig"
SANDBOX_UID: Final = 10001
SECCOMP_PROFILE: Final = "/etc/slas/seccomp-sandbox.json"

_SECRET_ENV = re.compile(
    r"(?i)(token|passw|secret|api[_-]?key|private[_-]?key|credential|ssh_auth_sock|"
    r"git_askpass|netrc|pat\b)"
)
FORBIDDEN_MOUNT_PARTS: Final[tuple[str, ...]] = (
    "docker.sock",
    "podman.sock",
    "/run/podman",
    "/var/run/docker",
    "/tmp/.X11-unix",  # noqa: S108 — the host display socket we refuse to mount (INV-4)
    "/dev/input",
    "/dev/dri",
    "/.ssh",
    ".git-credentials",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "/etc/shadow",
)


class HardeningError(RuntimeError):
    """Not a ValueError on purpose: pydantic would fold it into a ValidationError and the
    three-part message would be lost on the way to the person."""

    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class Resources(SlasModel):
    cpus: float = Field(default=2.0, gt=0, le=64)
    memory: str = Field(default="4g", pattern=r"^\d+[kmg]$")
    pids: int = Field(default=512, ge=32, le=8192)
    tmp_size: str = Field(default="512m", pattern=r"^\d+[kmg]$")
    #: Scratch overlay quota, enforced by the manager when it reaps and reports.
    scratch_size: str = Field(default="2g", pattern=r"^\d+[kmg]$")


class Mount(SlasModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1, pattern=r"^/")
    mode: Literal["ro", "rw"]


class SandboxSpec(SlasModel):
    name: str = Field(pattern=r"^slas-sbx-[a-z0-9][a-z0-9-]*$")
    image: str = Field(min_length=1)
    runtime: Runtime
    #: The person the sandbox works for; identity only, never a credential.
    user: str = Field(min_length=1)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    mounts: list[Mount] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    resources: Resources = Field(default_factory=Resources)
    ttl_s: int = Field(default=3600, ge=60, le=86400)
    #: Always none. The field exists so a reader sees it, not so anyone can change it.
    network: Literal["none"] = "none"

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        # Outside the validator machinery on purpose: the three-part HardeningError reaches
        # the caller as itself, not wrapped in a ValidationError.
        check_hardening(self)

    def sentence(self) -> str:
        runtime = {
            "runsc": "gVisor",
            "kata-fc": "a Kata micro-VM on Firecracker",
            "runc": "hardened runc",
        }[self.runtime]
        return (
            f"Sandbox {self.name} for {self.slug}: {runtime}, no network, read-only system, "
            f"{self.resources.cpus:g} CPUs, {self.resources.memory} memory, "
            f"closes after {self.ttl_s // 60} minutes idle."
        )


def check_hardening(spec: SandboxSpec) -> None:
    """Refuse anything a sandbox must never have. Called at construction and before any run."""
    if spec.image.endswith(":latest") or ("@sha256:" not in spec.image and ":" not in spec.image):
        raise HardeningError(
            ThreePartMessage(
                f"The sandbox image {spec.image} is not pinned.",
                "Images are pinned by tag or digest, never `latest` (INV-8).",
                "Use the image the toolchain resolver names.",
            )
        )
    for key in spec.env:
        if _SECRET_ENV.search(key):
            raise HardeningError(
                ThreePartMessage(
                    f"The sandbox environment would carry {key}.",
                    "A sandbox never holds a credential or a path to one (INV-14, INV-5).",
                    "Remove it; remote Git operations run in git-broker, which resolves "
                    "credentials at dispatch.",
                )
            )
    targets = [mount.target for mount in spec.mounts]
    if sorted(targets) != sorted([WORKSPACE, SCRATCH, GITCONFIG_TARGET]):
        raise HardeningError(
            ThreePartMessage(
                "The sandbox mounts are not the three the platform allows.",
                f"A sandbox mounts exactly {WORKSPACE} (project, rw), {SCRATCH} (scratch, rw) "
                f"and {GITCONFIG_TARGET} (identity, ro).",
                "Do not add mounts; the sandbox reaches nothing else by design (INV-4).",
            )
        )
    for mount in spec.mounts:
        lowered = mount.source.lower()
        for part in FORBIDDEN_MOUNT_PARTS:
            if part.lower() in lowered:
                raise HardeningError(
                    ThreePartMessage(
                        f"The sandbox would mount {mount.source}.",
                        "Runtime sockets, the host display, input devices, SSH keys and "
                        "credential files never enter a sandbox (INV-4, INV-14).",
                        "Remove the mount.",
                    )
                )
        if mount.target == GITCONFIG_TARGET and mount.mode != "ro":
            raise HardeningError(
                ThreePartMessage(
                    "The git identity would be mounted read-write.",
                    "The identity file is owned by the platform and only read by the sandbox.",
                    "Mount it read-only.",
                )
            )


def default_env(*, language: str) -> dict[str, str]:
    return {
        "HOME": SCRATCH,
        "GIT_CONFIG_GLOBAL": GITCONFIG_TARGET,
        "GIT_TERMINAL_PROMPT": "0",
        "SLAS_LANGUAGE": language,
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",  # noqa: S108 — the container's private tmpfs, not the host
    }


def podman_argv(spec: SandboxSpec) -> list[str]:
    """`podman run` for this spec: rootless, detached, idle until `exec` gives it work."""
    check_hardening(spec)  # a spec mutated after construction still cannot run unhardened
    argv: list[str] = [
        "podman",
        "run",
        "--detach",
        "--name",
        spec.name,
        "--runtime",
        spec.runtime,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size={spec.resources.tmp_size}",  # noqa: S108 — container tmpfs
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(spec.resources.pids),
        "--memory",
        spec.resources.memory,
        "--cpus",
        f"{spec.resources.cpus:g}",
        "--user",
        f"{SANDBOX_UID}:{SANDBOX_UID}",
        "--workdir",
        WORKSPACE,
        "--label",
        f"slas.user={spec.user}",
        "--label",
        f"slas.project={spec.slug}",
        "--label",
        f"slas.ttl_s={spec.ttl_s}",
    ]
    if spec.runtime == "runc":
        # Without gVisor the kernel is shared: keep the container in its own user namespace
        # and under a seccomp profile as well.
        argv += ["--userns", "keep-id", "--security-opt", f"seccomp={SECCOMP_PROFILE}"]
    for mount in spec.mounts:
        argv += ["--volume", f"{mount.source}:{mount.target}:{mount.mode}"]
    for key in sorted(spec.env):
        argv += ["--env", f"{key}={spec.env[key]}"]
    argv += [spec.image, "sleep", "infinity"]
    return argv


def exec_argv(name: str, argv: list[str], *, cwd: str = WORKSPACE) -> list[str]:
    """`podman exec` for one command inside a running sandbox; argv only, never a shell line."""
    if not argv:
        raise ValueError("argv must not be empty")
    return [
        "podman",
        "exec",
        "--workdir",
        cwd,
        "--user",
        f"{SANDBOX_UID}:{SANDBOX_UID}",
        name,
        *argv,
    ]
