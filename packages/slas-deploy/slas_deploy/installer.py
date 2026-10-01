"""The parts of `install.sh` that are easier to get right in Python (ADR-0003, ADR-0012):
writing `.env`, generating the secret files, checking the image lock and a bundle manifest,
and describing the compose command for a profile. `install.sh` calls
`python -m slas_deploy.installer <command>`; every command is idempotent and prints one
sentence per thing it did.

    write-env       fill ${SLAS_DATA_ROOT}/.env from config/.env.example, keeping what is set
    secrets         generate the missing secret files under ${SLAS_DATA_ROOT}/secrets (0600)
    check-lock      refuse to continue while the profile starts an unpinned image
    check-manifest  compare a bundle's manifest with the lock
    compose-files   the -f arguments for the profile and the overlays .env asks for
    build-images    --build (ADR-0014): pull, build, tag; write the filled lock to the data root
    build-sandbox-images
                    --build: build the sandbox images the sandbox manager lists, write their
                    lock beside the image lock and the toolchain manifest (contract §4, §9)
    runtime-socket  which container-runtime socket .env should name (ADR-0015): the configured
                    one, else Podman's when it exists, else Docker's
    data-dirs       create the data-root directories the services bind-mount, as this user
    unhealthy       read `docker compose ps --format json`; print the services not healthy yet
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO

from slas_deploy import sandbox_images
from slas_deploy.build import BuildError, LocalRunner, Runner, build_images, describe, plan
from slas_deploy.compose import (
    DATA_DIRECTORIES,
    PROD_SECRETS,
    QUICKSTART_SECRETS,
    RUNTIME_SOCKET_HOLDERS,
)
from slas_deploy.images import (
    AGENT_NOUNS,
    AGENTS,
    DEFAULT_AGENTS,
    AgentsError,
    BundleManifest,
    ImageLock,
    LockError,
    Profile,
    check_lock,
    check_manifest,
    compose_profiles,
    parse_agents,
    parse_lock_json,
    profiles_sentence,
    registry_for,
    render_lock_json,
    services_off_for,
)
from slas_schemas.envfile import EnvFile, generate_secret, read_env, write_atomic

EXIT_OK: Final = 0
EXIT_PROBLEMS: Final = 1

#: Keys install.sh fills in for every profile; the value is computed when None.
COMMON_KEYS: Final[dict[str, str | None]] = {
    "SLAS_PROFILE": None,
    "SLAS_DATA_ROOT": None,
    "SLAS_VERSION": None,
    "SLAS_REGISTRY": None,
    "SLAS_UID": None,
    "SLAS_GID": None,
    "SLAS_TLS_NAMES": None,
    "SLAS_PUBLIC_HOST": None,
    "SLAS_PUBLIC_HOST_PINNED": None,
}

PROD_KEYS: Final[dict[str, str]] = {
    "SLAS_AUTH_MODES": "builtin,oidc",
    "SLAS_OIDC_ISSUER": "https://${SLAS_PUBLIC_HOST}/auth/realms/slas",
    "SLAS_OIDC_CLIENT_ID": "slas-webui",
    "SLAS_COSIGN_KEY": "config/cosign.pub",
    "SLAS_SANDBOX_TIER": "kata",
    "SLAS_BACKUP_FULL_CRON": "0",
    "SLAS_BACKUP_DIFF_CRON": "2",
    "SLAS_BACKUP_RETENTION_DAYS": "90",
    "SLAS_ARTIFACT_RETENTION_DAYS": "30",
    "SLAS_FACTORY_IFACE": "",
    "SLAS_FACTORY_SUBNET": "",
    "SLAS_FACTORY_GATEWAY": "",
    "SLAS_FACTORY_EXECUTOR_IP": "",
}

#: Secret files whose content is derived, not random.
DERIVED_SECRETS: Final[frozenset[str]] = frozenset({"redis.conf"})
#: Secret files created empty: a person fills them in when needed (the hub token, ADR-0018).
EMPTY_SECRETS: Final[frozenset[str]] = frozenset({"hf_token"})

#: Where the two engines put their socket on a stock host. Rootless Podman's lives under
#: $XDG_RUNTIME_DIR; the compose default and the doctor look at the rootful path.
PODMAN_SOCKET: Final = "/run/podman/podman.sock"
DOCKER_SOCKET: Final = "/var/run/docker.sock"


@dataclass(frozen=True)
class RuntimeSocketChoice:
    """What .env should say for SLAS_RUNTIME_SOCKET, and the sentence that explains it."""

    path: str  # "" keeps the compose default (Podman's path)
    sentence: str

    @property
    def key_value(self) -> str:
        return f"SLAS_RUNTIME_SOCKET={self.path}"


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def choose_runtime_socket(
    configured: str, *, podman: str = PODMAN_SOCKET, docker: str = DOCKER_SOCKET
) -> RuntimeSocketChoice:
    """ADR-0015 (amended 2026-09-18): the socket of the engine that holds the images. The stack
    runs on `docker compose`, and `install.sh --build` and `docker load` put every image in
    Docker's store, so Docker's socket wins whenever it exists; a sandbox or vLLM container
    created through Podman's socket on such a host fails with "image not known". Podman's
    socket (the compose default) serves only a host without Docker. A value a person set is
    kept as it is."""
    holders = " and ".join(sorted(RUNTIME_SOCKET_HOLDERS))
    if configured.strip():
        return RuntimeSocketChoice(
            configured.strip(),
            f"SLAS_RUNTIME_SOCKET is set to {configured.strip()}; {holders} see that socket "
            "and nothing else does (INV-4).",
        )
    if _is_socket(docker):
        return RuntimeSocketChoice(
            docker,
            f"Docker's socket {docker} serves the container runtime, so SLAS_RUNTIME_SOCKET="
            f"{docker} in .env points {holders} at it (the images install.sh builds and loads "
            "live in Docker's store); nothing else sees it (INV-4).",
        )
    if _is_socket(podman):
        return RuntimeSocketChoice(
            "",
            f"No Docker socket at {docker}, so Podman's socket {podman} serves the container "
            f"runtime; {holders} see it and nothing else does (INV-4).",
        )
    return RuntimeSocketChoice(
        "",
        f"Neither {podman} nor {docker} exists yet; .env keeps the Podman default and "
        f"{holders} stay unhealthy until a runtime socket is there.",
    )


def create_data_dirs(root: Path, directories: Sequence[str] = DATA_DIRECTORIES) -> list[str]:
    """Create every data-root directory the services bind-mount, as the calling user, so the
    engine never creates one root-owned when it mounts a missing path."""
    created: list[str] = []
    for relative in directories:
        path = root / relative
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(relative)
    return created


# --- the GPUs -------------------------------------------------------------------------------

#: What the installer asks nvidia-smi: one line per GPU, `index, memory.total (MiB)`.
NVIDIA_SMI_QUERY: Final[tuple[str, ...]] = (
    "nvidia-smi",
    "--query-gpu=index,memory.total",
    "--format=csv,noheader,nounits",
)
#: The share of a GPU's memory the placement may plan against: the CUDA context, the
#: activation workspace and fragmentation take the rest (288 GB → 270 GiB, the B300 default).
VRAM_HEADROOM: Final = 0.94


@dataclass(frozen=True)
class GpuInventory:
    """The GPUs nvidia-smi sees, as .env should record them for the model manager."""

    ids: tuple[int, ...]
    #: GiB per GPU the placement plans against: the smallest card, minus the headroom.
    vram_gib: int | None
    sentence: str

    @property
    def ids_text(self) -> str:
        return ",".join(str(i) for i in self.ids)


def parse_nvidia_smi(text: str) -> list[tuple[int, int]]:
    """`0, 81559` lines → [(index, MiB)]; anything else on a line is skipped."""
    gpus: list[tuple[int, int]] = []
    for line in text.splitlines():
        index, sep, memory = line.partition(",")
        if not sep:
            continue
        try:
            gpus.append((int(index.strip()), int(float(memory.strip()))))
        except ValueError:
            continue
    return gpus


def detect_gpus(run: Callable[[Sequence[str]], str | None]) -> GpuInventory:
    """Ask nvidia-smi which GPUs this host has; `run` returns its stdout or None.

    SLAS_GPU_IDS and SLAS_GPU_VRAM_GIB used to be guesses (four GPUs of 270 GiB, the reference
    host); on any other host the model manager then placed instances on GPUs that do not exist
    or planned against memory the cards do not have, and every vLLM instance failed.
    """
    output = run(NVIDIA_SMI_QUERY)
    gpus = parse_nvidia_smi(output or "")
    if not gpus:
        return GpuInventory(
            (),
            None,
            "nvidia-smi found no GPU (or is not installed), so SLAS_GPU_IDS and "
            "SLAS_GPU_VRAM_GIB in .env stay as they are; the model manager needs them to "
            "match the host before any model instance can start.",
        )
    ids = tuple(index for index, _ in gpus)
    smallest_mib = min(memory for _, memory in gpus)
    vram_gib = int(smallest_mib / 1024 * VRAM_HEADROOM)
    count = len(gpus)
    shown = f"{smallest_mib / 1024:.0f} GiB"
    return GpuInventory(
        ids,
        vram_gib,
        f"{count} GPU{'s' if count != 1 else ''} found ({shown} each"
        + ("" if len({m for _, m in gpus}) == 1 else ", the smallest")
        + f"): .env gets SLAS_GPU_IDS={','.join(str(i) for i in ids)} and "
        f"SLAS_GPU_VRAM_GIB={vram_gib} (the memory the model manager plans against, with "
        f"{int((1 - VRAM_HEADROOM) * 100)} % headroom); a value you set by hand is kept.",
    )


def run_nvidia_smi(argv: Sequence[str]) -> str | None:
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [executable, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


# --- the model instances after the start -----------------------------------------------------

EXIT_LOADING: Final = 2
EXIT_UNKNOWN: Final = 3


def instances_report(status_json: str, *, role: str = "coder") -> tuple[int, str]:
    """Read the model manager's `GET /v1/status`; return (exit code, what to print).

    0: an instance serves `role` and is healthy. EXIT_LOADING: not yet, and some instance is
    still loading. EXIT_PROBLEMS: nothing is loading and the role has no healthy instance —
    the sentences say why (a crash with its log tail, no room, no GPU). EXIT_UNKNOWN: the
    status could not be read.
    """
    text = status_json.strip()
    if not text:
        return EXIT_UNKNOWN, (
            "The model manager did not answer, so the state of the model instances is unknown. "
            "Likely cause: it is still starting. What to do: in a minute open the Models page, "
            "or run `docker compose -p slas logs model-manager` on the host."
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return EXIT_UNKNOWN, (
            "The model manager answered something that is not its status. What to do: run "
            "`docker compose -p slas logs model-manager` on the host."
        )
    rows = payload.get("instances") if isinstance(payload, dict) else None
    rows = [r for r in rows or [] if isinstance(r, dict)]
    lines = [
        str(payload.get("sentence") or "").strip() or "The model manager reported no sentence."
    ]
    for row in rows:
        name = str(row.get("name") or "?")
        served = f" ({row['role']})" if row.get("role") else " (voter)"
        state = str(row.get("state") or "?")
        sentence = str(row.get("sentence") or "").strip().replace("\n", "\n      ")
        lines.append(f"  {name}{served}: {state} — {sentence}")
    coder = [r for r in rows if r.get("role") == role]
    if any(r.get("state") == "healthy" for r in coder):
        return EXIT_OK, "\n".join(lines)
    if any(r.get("state") == "starting" for r in rows):
        return EXIT_LOADING, "\n".join(lines)
    if not coder:
        lines.append(
            f"No instance is planned for the {role} role. Likely cause: Models/models.yaml "
            f"names no model for it, or the model does not fit this host's GPUs. What to do: "
            "on the Models page give the role a model that fits, or add a smaller one."
        )
    return EXIT_PROBLEMS, "\n".join(lines)


def merge_names(existing: str, wanted: str) -> str:
    """`SLAS_TLS_NAMES` as the union of what the file says and what this install needs, the
    file's order first, no duplicates: a name added by hand stays, and a name the installer
    learnt since (the host's address for browsers that use the IP) joins it."""
    merged: list[str] = []
    for part in (*existing.split(","), *wanted.split(",")):
        name = part.strip()
        if name and name not in merged:
            merged.append(name)
    return ",".join(merged)


def write_env(
    *,
    example: Path,
    target: Path,
    profile: Profile,
    data_root: Path,
    version: str,
    registry: str,
    uid: int,
    gid: int,
    tls_names: str,
    public_host: str,
    public_host_chosen: bool = False,
    runtime_socket: str | None = None,
    agents: Sequence[str] = DEFAULT_AGENTS,
    gpu_ids: str | None = None,
    gpu_vram_gib: str | None = None,
) -> list[str]:
    """Fill the keys the profile needs, never touching a key a person already set.

    Two keys behave differently. `SLAS_TLS_NAMES` is merged: the edge's certificate can only
    gain names. `SLAS_PUBLIC_HOST` follows the installer's value (the host's address on the
    default route, so a DHCP change is followed on the next run) until a run with
    `public_host_chosen` (`--public-host`, `SLAS_PUBLIC_HOST`) pins it: that writes
    `SLAS_PUBLIC_HOST_PINNED=yes`, and later runs leave the name alone until the person clears
    that key."""
    defaults = EnvFile.parse(example.read_text(encoding="utf-8"))
    env = read_env(target) if target.is_file() else EnvFile.parse(defaults.render())
    changed: list[str] = []

    def at_default(key: str) -> bool:
        current = env.get(key)
        return current is None or current == "" or current == (defaults.get(key) or "")

    merged_names = merge_names(env.get("SLAS_TLS_NAMES") or "", tls_names)
    if env.get("SLAS_TLS_NAMES") != merged_names:
        env.set("SLAS_TLS_NAMES", merged_names, under_marker="# --- prod profile (ADR-0012) ---")
        changed.append("SLAS_TLS_NAMES")
    pinned = (env.get("SLAS_PUBLIC_HOST_PINNED") or "").strip().lower() in ("yes", "1", "true")
    if (public_host_chosen or not pinned) and env.get("SLAS_PUBLIC_HOST") != public_host:
        env.set("SLAS_PUBLIC_HOST", public_host, under_marker="# --- prod profile (ADR-0012) ---")
        changed.append("SLAS_PUBLIC_HOST")
    if public_host_chosen and not pinned:
        env.set("SLAS_PUBLIC_HOST_PINNED", "yes", under_marker="# --- prod profile (ADR-0012) ---")
        changed.append("SLAS_PUBLIC_HOST_PINNED")

    # The installer owns these: they describe this install, not a choice a person makes.
    owned = {
        "SLAS_PROFILE": profile,
        "SLAS_DATA_ROOT": str(data_root),
        "SLAS_VERSION": version,
        "SLAS_UID": str(uid),
        "SLAS_GID": str(gid),
        # Which agents this install starts (ADR-0017); --agents decides, the file follows.
        "SLAS_AGENTS": ",".join(agents),
    }
    for key, value in owned.items():
        if env.get(key) != value:
            env.set(key, value)
            changed.append(key)
    # These a person may have set by hand; the installer fills them only while they are at
    # the template's default.
    settable = {"SLAS_REGISTRY": registry}
    if runtime_socket:
        settable["SLAS_RUNTIME_SOCKET"] = runtime_socket
    # The GPUs nvidia-smi found (`detect_gpus`): filled while the keys are at the template's
    # guesses, kept once a person set them.
    if gpu_ids:
        settable["SLAS_GPU_IDS"] = gpu_ids
    if gpu_vram_gib:
        settable["SLAS_GPU_VRAM_GIB"] = gpu_vram_gib
    if profile == "prod":
        settable.update(PROD_KEYS)
    for key, value in settable.items():
        if at_default(key) and env.get(key) != value:
            env.set(key, value, under_marker="# --- prod profile (ADR-0012) ---")
            changed.append(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(target, env.render(), mode=0o600)
    return changed


def write_secrets(secrets_dir: Path, profile: Profile) -> list[str]:
    """Create every secret file the profile's compose files mount, if missing (ADR-0003)."""
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(secrets_dir, 0o700)
    wanted = [*QUICKSTART_SECRETS, "grafana_admin_password"]
    if profile == "prod":
        wanted += list(PROD_SECRETS)
    created: list[str] = []
    for name in wanted:
        path = secrets_dir / name
        if path.exists():
            continue
        if name == "redis.conf":
            password = (secrets_dir / "redis_password").read_text(encoding="utf-8").strip()
            content = f"requirepass {password}\nprotected-mode yes\nsave 900 1\nappendonly yes\n"
        elif name == "admin-initial-password":
            content = generate_secret(18)
        elif name in EMPTY_SECRETS:
            content = ""
        else:
            content = generate_secret(32)
        world_readable = name in {
            "postgres_password",
            "redis_password",
            "redis.conf",
            "minio_root_password",
            "grafana_admin_password",
            "keycloak_db_password",
            "keycloak_admin_password",
            "pgbackrest_s3_key",
            "pgbackrest_s3_secret",
        }
        write_atomic(path, content + "\n", mode=0o644 if world_readable else 0o600)
        created.append(name)
    return created


def compose_files(profile: Profile, env: EnvFile, compose_dir: Path) -> list[str]:
    files = [str(compose_dir / "docker-compose.yml")]
    if profile == "prod":
        files.append(str(compose_dir / "prod.override.yml"))
    if env.get("SLAS_LAB_IFACE"):
        files.append(str(compose_dir / "macvlan.override.yml"))
    if env.get("SLAS_FACTORY_IFACE"):
        files.append(str(compose_dir / "macvlan-factory.override.yml"))
    return files


def _load_lock(path: Path) -> ImageLock:
    return parse_lock_json(path.read_text(encoding="utf-8"))


def unhealthy_services(ps_json: str, *, ignore: Sequence[str] = ()) -> list[str]:
    """The services `docker compose ps --format json` shows as not healthy yet.

    Accepts one JSON object per line (compose v2.21+) or a JSON array. Healthy means state
    `running` with health `healthy` or no healthcheck; `restarting`, `created`, `paused`,
    `dead`, health `starting` or `unhealthy` are not. A container that exited with 0 is a
    finished one-shot job (minio-init) and is fine; a non-zero exit is not. `ignore` names
    the services this install does not start (ADR-0017): `ps --all` still lists a container
    of theirs left by an earlier install, and it must not hold the wait.
    """
    skipped = set(ignore)
    text = ps_json.strip()
    if not text:
        return []
    rows: list[dict[str, Any]]
    try:
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    unhealthy: list[str] = []
    for row in rows:
        name = str(row.get("Service") or row.get("Name") or "unknown")
        if name in skipped:
            continue
        state = str(row.get("State") or "").lower()
        health = str(row.get("Health") or "").lower()
        exit_code = row.get("ExitCode", 0)
        if state == "exited" and str(exit_code) == "0":
            continue
        if state != "running" or health not in ("", "healthy"):
            unhealthy.append(name)
    return unhealthy


def run_build(
    *,
    lock_path: Path,
    profile: Profile,
    registry: str,
    version: str,
    repo: Path,
    out_path: Path,
    dry_run: bool,
    out: TextIO,
    runner: Runner | None = None,
    agents: Sequence[str] = DEFAULT_AGENTS,
) -> int:
    lock = _load_lock(lock_path)
    if dry_run:
        describe(
            plan(lock, profile, registry=registry, version=version, repo=repo, agents=agents),
            out=out,
            lock_path=out_path,
        )
        return EXIT_OK
    try:
        filled = build_images(
            lock,
            profile,
            registry=registry,
            version=version,
            repo=repo,
            runner=runner or LocalRunner(),
            out=out,
            agents=agents,
        )
    except BuildError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out_path, render_lock_json(filled), mode=0o644)
    wanted = filled.for_profile(profile, agents)
    built = sum(1 for image in wanted if image.first_party)
    out.write(
        f"Wrote the filled image lock to {out_path}: {built} images built from this checkout, "
        f"{len(wanted) - built} pulled and tagged for {registry}.\n"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="slas_deploy.installer")
    commands = parser.add_subparsers(dest="command", required=True)

    env_cmd = commands.add_parser("write-env")
    env_cmd.add_argument("--example", required=True)
    env_cmd.add_argument("--target", required=True)
    env_cmd.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    env_cmd.add_argument("--data-root", required=True)
    env_cmd.add_argument("--version", required=True)
    env_cmd.add_argument("--registry", required=True)
    env_cmd.add_argument("--uid", type=int, required=True)
    env_cmd.add_argument("--gid", type=int, required=True)
    env_cmd.add_argument("--tls-names", required=True)
    env_cmd.add_argument("--public-host", required=True)
    env_cmd.add_argument(
        "--public-host-chosen",
        action="store_true",
        help="the person named --public-host on this run; it replaces what .env says",
    )
    env_cmd.add_argument(
        "--runtime-socket",
        default="",
        help="host path of the container-runtime socket to record (empty keeps the default)",
    )
    env_cmd.add_argument("--agents", default="", help="comma list of agents to start (ADR-0017)")
    env_cmd.add_argument("--gpu-ids", default="", help="SLAS_GPU_IDS as nvidia-smi found them")
    env_cmd.add_argument("--gpu-vram-gib", default="", help="SLAS_GPU_VRAM_GIB from nvidia-smi")

    gpus_cmd = commands.add_parser("gpus", help="what nvidia-smi sees, as JSON for .env")
    gpus_cmd.add_argument("--json", action="store_true")

    instances_cmd = commands.add_parser(
        "instances", help="stdin: the model manager's GET /v1/status; exit 0 when the role is up"
    )
    instances_cmd.add_argument("--role", default="coder")

    sock = commands.add_parser("runtime-socket")
    sock.add_argument("--configured", default="", help="SLAS_RUNTIME_SOCKET as set today")
    sock.add_argument("--podman", default=PODMAN_SOCKET)
    sock.add_argument("--docker", default=DOCKER_SOCKET)
    sock.add_argument("--json", action="store_true")

    dirs = commands.add_parser("data-dirs")
    dirs.add_argument("--root", required=True)
    dirs.add_argument("--dry-run", action="store_true")

    sandbox = commands.add_parser("build-sandbox-images")
    sandbox.add_argument("--python", required=True, help="the interpreter with the packages")
    sandbox.add_argument("--registry", required=True)
    sandbox.add_argument("--repo", required=True)
    sandbox.add_argument("--out", required=True, help="where the sandbox image lock is written")
    sandbox.add_argument("--manifest", required=True, help="the toolchain manifest to write")
    sandbox.add_argument("--dry-run", action="store_true")

    sec = commands.add_parser("secrets")
    sec.add_argument("--dir", required=True)
    sec.add_argument("--profile", choices=("quickstart", "prod"), required=True)

    lock = commands.add_parser("check-lock")
    lock.add_argument("--lock", required=True)
    lock.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    lock.add_argument("--agents", default="", help="comma list of agents to start (ADR-0017)")

    agents_cmd = commands.add_parser("agents", help="validate --agents; print profiles as JSON")
    agents_cmd.add_argument("--agents", default="")
    agents_cmd.add_argument(
        "--profile",
        choices=("quickstart", "prod"),
        default="quickstart",
        help="the install profile: quickstart adds the `fetch` compose profile (ADR-0018)",
    )

    man = commands.add_parser("check-manifest")
    man.add_argument("--lock", required=True)
    man.add_argument("--manifest", required=True)
    man.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    man.add_argument("--registry", default=None)

    cf = commands.add_parser("compose-files")
    cf.add_argument("--env", required=True)
    cf.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    cf.add_argument("--compose-dir", required=True)

    build = commands.add_parser("build-images")
    build.add_argument("--lock", required=True, help="the unpinned lock in the repository")
    build.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    build.add_argument("--registry", required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--repo", required=True, help="the checkout: the build context")
    build.add_argument("--out", required=True, help="where the filled lock is written")
    build.add_argument("--dry-run", action="store_true")
    build.add_argument("--agents", default="", help="comma list of agents to start (ADR-0017)")

    unhealthy = commands.add_parser("unhealthy", help="stdin: docker compose ps --format json")
    unhealthy.add_argument(
        "--ignore", default="", help="comma list of services this install does not start"
    )

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        agents = parse_agents(getattr(args, "agents", ""))
    except AgentsError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    if args.command == "agents":
        profiles = compose_profiles(agents, args.profile)
        off = [name for name in AGENTS if name in AGENT_NOUNS and name not in agents]
        sentence = "Agents: " + ", ".join(agents) + "."
        if off:
            nouns = [AGENT_NOUNS[name] for name in off]
            listed = nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " and " + nouns[-1]
            verb = "stays" if len(off) == 1 else "stay"
            sentence += (
                f" {listed[0].upper()}{listed[1:]} {verb} off; enable "
                f"{'it' if len(off) == 1 else 'them'} with --agents {','.join(AGENTS)}."
            )
        else:
            sentence += " Every optional part starts."
        out.write(
            json.dumps(
                {
                    "agents": list(agents),
                    "profiles": profiles,
                    "profiles_sentence": profiles_sentence(profiles),
                    "off_services": services_off_for(agents, args.profile),
                    "sentence": sentence,
                }
            )
            + "\n"
        )
        return EXIT_OK
    if args.command == "build-images":
        return run_build(
            lock_path=Path(args.lock),
            profile=args.profile,
            registry=args.registry,
            version=args.version,
            repo=Path(args.repo),
            out_path=Path(args.out),
            dry_run=args.dry_run,
            out=out,
            agents=agents,
        )
    if args.command == "build-sandbox-images":
        return sandbox_images.run(
            python=args.python,
            registry=args.registry,
            repo=Path(args.repo),
            lock_out=Path(args.out),
            manifest_out=Path(args.manifest),
            dry_run=args.dry_run,
            runner=LocalRunner(),
            out=out,
        )
    if args.command == "runtime-socket":
        choice = choose_runtime_socket(args.configured, podman=args.podman, docker=args.docker)
        if args.json:
            out.write(json.dumps({"path": choice.path, "sentence": choice.sentence}) + "\n")
        else:
            out.write(choice.path + "\n" + choice.sentence + "\n")
        return EXIT_OK
    if args.command == "data-dirs":
        root = Path(args.root)
        listed = ", ".join(DATA_DIRECTORIES)
        if args.dry_run:
            out.write(f"Would create the missing data directories under {root}: {listed}.\n")
            return EXIT_OK
        created = create_data_dirs(root)
        if created:
            out.write(
                f"Created {len(created)} data "
                f"{'directory' if len(created) == 1 else 'directories'} under {root} as "
                f"uid {os.getuid()}: {', '.join(created)}.\n"
            )
        else:
            out.write(f"Every data directory under {root} exists.\n")
        return EXIT_OK
    if args.command == "gpus":
        inventory = detect_gpus(run_nvidia_smi)
        if args.json:
            out.write(
                json.dumps(
                    {
                        "ids": inventory.ids_text,
                        "vram_gib": "" if inventory.vram_gib is None else str(inventory.vram_gib),
                        "sentence": inventory.sentence,
                    }
                )
                + "\n"
            )
        else:
            out.write(inventory.sentence + "\n")
        return EXIT_OK
    if args.command == "instances":
        code, report = instances_report(sys.stdin.read(), role=args.role)
        out.write(report + "\n")
        return code
    if args.command == "unhealthy":
        ignored = [name.strip() for name in args.ignore.split(",") if name.strip()]
        out.write(" ".join(unhealthy_services(sys.stdin.read(), ignore=ignored)) + "\n")
        return EXIT_OK
    if args.command == "write-env":
        changed = write_env(
            example=Path(args.example),
            target=Path(args.target),
            profile=args.profile,
            data_root=Path(args.data_root),
            version=args.version,
            registry=args.registry,
            uid=args.uid,
            gid=args.gid,
            tls_names=args.tls_names,
            public_host=args.public_host,
            public_host_chosen=args.public_host_chosen,
            runtime_socket=args.runtime_socket or None,
            agents=agents,
            gpu_ids=args.gpu_ids or None,
            gpu_vram_gib=args.gpu_vram_gib or None,
        )
        out.write(
            f"{args.target}: {'set ' + ', '.join(changed) if changed else 'nothing to change'}.\n"
        )
        return EXIT_OK
    if args.command == "secrets":
        created = write_secrets(Path(args.dir), args.profile)
        what = "created " + ", ".join(created) if created else "every secret file exists"
        out.write(f"{args.dir}: {what}.\n")
        return EXIT_OK
    if args.command == "check-lock":
        try:
            images = check_lock(_load_lock(Path(args.lock)), args.profile, agents)
        except LockError as exc:
            out.write(exc.message.render() + "\n")
            return EXIT_PROBLEMS
        out.write(
            f"All {len(images)} images the {args.profile} profile starts are pinned by digest.\n"
        )
        return EXIT_OK
    if args.command == "check-manifest":
        try:
            manifest = BundleManifest.model_validate_json(
                Path(args.manifest).read_text(encoding="utf-8")
            )
            registry = args.registry or registry_for(args.profile, os.environ)
            problems = check_manifest(
                _load_lock(Path(args.lock)), manifest, args.profile, registry=registry
            )
        except LockError as exc:
            out.write(exc.message.render() + "\n")
            return EXIT_PROBLEMS
        if problems:
            out.write(
                "The bundle does not match the lock. "
                + " ".join(f"{p}." for p in problems)
                + " Nothing was loaded; get the bundle that belongs to this release.\n"
            )
            return EXIT_PROBLEMS
        out.write(
            f"The bundle carries every image the {args.profile} profile starts, with the IDs "
            "the lock records.\n"
        )
        return EXIT_OK
    env = read_env(Path(args.env))
    files = compose_files(args.profile, env, Path(args.compose_dir))
    out.write(json.dumps(files) + "\n")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
