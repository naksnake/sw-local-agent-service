"""Process settings for the sandbox manager, read once from the environment (contract §4).

| Variable | Default | Meaning |
|---|---|---|
| `SLAS_RUNTIME_SOCKET` | `/run/podman/podman.sock` | the runtime socket, as mounted here |
| `SLAS_DATA_ROOT` | `/data` | the data root as this container sees it (`Coding/` under it) |
| `SLAS_HOST_DATA_ROOT` | `/AI/Agent` | the same root on the host, for the sandboxes' bind mounts |
| `DEFAULT_RUNTIME` | `runsc` | the OCI runtime sandboxes ask for (`runsc`, `runc`, `kata-fc`) |
| `SANDBOX_TIER` | `gvisor` | `gvisor` or `kata` (prod) |
| `SLAS_SANDBOX_REGISTRY` | `local` | registry label: `<registry>/slas/sandbox-<lang>:<ver>` |
| `SLAS_TOOLCHAIN_MANIFEST` | `/data/Toolchains/manifest.json` | written by `install.sh --build` |
| `SLAS_PROFILE` | `quickstart` | `quickstart` falls back to hardened runc; `prod` needs gVisor |
| `SLAS_BIND` | `0.0.0.0:8000` | where uvicorn listens |
| `SLAS_MAX_SESSIONS_PER_USER` | `3` | sandboxes one person may hold open |
| `SLAS_SANDBOX_TTL_S` | `3600` | idle time before a sandbox closes |
| `PIDS_LIMIT` | `512` | pids limit per sandbox (compose sets it) |
| `SLAS_SANDBOX_CPUS` / `SLAS_SANDBOX_MEMORY` | `2` / `4g` | CPU and memory cap per sandbox |
| `SLAS_REAP_INTERVAL_S` | `60` | how often idle sandboxes are closed |
| `SLAS_SECCOMP_PROFILE` | `/etc/slas/seccomp-sandbox.json` | seccomp profile for hardened runc |

Standard library only: settings are facts, never secrets (the sandbox manager holds none).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, get_args

from slas_sandbox_manager.manager import Profile
from slas_sandbox_manager.runtime import DEFAULT_SANDBOX_USER
from slas_sandbox_manager.spec import SECCOMP_PROFILE, Runtime, Tier
from slas_sandbox_manager.toolchains import DEFAULT_REGISTRY, REGISTRY_ENV

DEFAULT_SOCKET: Final = "/run/podman/podman.sock"
DEFAULT_HOST_DATA_ROOT: Final = "/AI/Agent"


class SettingsError(ValueError):
    """An environment value the service cannot start with; the message says which."""


def _choice(env: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = env.get(name, "").strip() or default
    if value not in allowed:
        raise SettingsError(
            f"{name} is {value!r}; it must be one of {', '.join(allowed)}. "
            "Fix it in .env and run `docker compose up -d` again."
        )
    return value


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} is {raw!r}; it must be a whole number.") from None
    if value < minimum:
        raise SettingsError(f"{name} is {value}; it must be at least {minimum}.")
    return value


_USER = re.compile(r"^[0-9]{1,10}:[0-9]{1,10}$")


def _user(raw: str) -> str:
    if not raw:
        return DEFAULT_SANDBOX_USER
    if not _USER.match(raw):
        raise SettingsError(f"SLAS_SANDBOX_USER is {raw!r}; it must be `uid:gid` as numbers.")
    return raw


@dataclass(frozen=True)
class Settings:
    runtime_socket: str = DEFAULT_SOCKET
    data_root: Path = Path("/data")
    host_data_root: Path = Path(DEFAULT_HOST_DATA_ROOT)
    default_runtime: Runtime = "runsc"
    tier: Tier = "gvisor"
    registry: str = DEFAULT_REGISTRY
    toolchain_manifest: Path = Path("/data/Toolchains/manifest.json")
    profile: Profile = "quickstart"
    bind: str = "0.0.0.0:8000"  # the container's own interface
    max_sessions_per_user: int = 3
    default_ttl_s: int = 3600
    pids_limit: int = 512
    cpus: float = 2.0
    memory: str = "4g"
    reap_interval_s: int = 60
    seccomp_profile: Path = Path(SECCOMP_PROFILE)
    #: `uid:gid` the sandboxes run as; compose sets the platform's data owner so the sandbox
    #: can write the project directory the services own (SLAS_SANDBOX_USER).
    sandbox_user: str = DEFAULT_SANDBOX_USER

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        data_root = Path(source.get("SLAS_DATA_ROOT", "").strip() or "/data")
        manifest = source.get("SLAS_TOOLCHAIN_MANIFEST", "").strip()
        default_runtime = _choice(source, "DEFAULT_RUNTIME", "runsc", get_args(Runtime))
        tier = _choice(source, "SANDBOX_TIER", "gvisor", get_args(Tier))
        profile = _choice(source, "SLAS_PROFILE", "quickstart", get_args(Profile))
        cpus_raw = source.get("SLAS_SANDBOX_CPUS", "").strip()
        try:
            cpus = float(cpus_raw) if cpus_raw else 2.0
        except ValueError:
            raise SettingsError(
                f"SLAS_SANDBOX_CPUS is {cpus_raw!r}; it must be a number."
            ) from None
        return cls(
            runtime_socket=source.get("SLAS_RUNTIME_SOCKET", "").strip() or DEFAULT_SOCKET,
            data_root=data_root,
            host_data_root=Path(
                source.get("SLAS_HOST_DATA_ROOT", "").strip() or DEFAULT_HOST_DATA_ROOT
            ),
            default_runtime=default_runtime,  # type: ignore[arg-type]  # checked by _choice
            tier=tier,  # type: ignore[arg-type]
            registry=source.get(REGISTRY_ENV, "").strip().rstrip("/") or DEFAULT_REGISTRY,
            toolchain_manifest=(
                Path(manifest) if manifest else data_root / "Toolchains" / "manifest.json"
            ),
            profile=profile,  # type: ignore[arg-type]
            bind=source.get("SLAS_BIND", "").strip() or "0.0.0.0:8000",
            max_sessions_per_user=_int(source, "SLAS_MAX_SESSIONS_PER_USER", 3),
            default_ttl_s=_int(source, "SLAS_SANDBOX_TTL_S", 3600, minimum=60),
            pids_limit=_int(source, "PIDS_LIMIT", 512, minimum=32),
            cpus=cpus,
            memory=source.get("SLAS_SANDBOX_MEMORY", "").strip() or "4g",
            reap_interval_s=_int(source, "SLAS_REAP_INTERVAL_S", 60),
            sandbox_user=_user(source.get("SLAS_SANDBOX_USER", "").strip()),
            seccomp_profile=Path(source.get("SLAS_SECCOMP_PROFILE", "").strip() or SECCOMP_PROFILE),
        )
