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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from slas_deploy.compose import PROD_SECRETS, QUICKSTART_SECRETS
from slas_deploy.images import (
    BundleManifest,
    ImageLock,
    LockError,
    Profile,
    check_lock,
    check_manifest,
    parse_lock_json,
    registry_for,
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
) -> list[str]:
    """Fill the keys the profile needs, never touching a key a person already set."""
    defaults = EnvFile.parse(example.read_text(encoding="utf-8"))
    env = read_env(target) if target.is_file() else EnvFile.parse(defaults.render())
    changed: list[str] = []

    def at_default(key: str) -> bool:
        current = env.get(key)
        return current is None or current == "" or current == (defaults.get(key) or "")

    # The installer owns these: they describe this install, not a choice a person makes.
    owned = {
        "SLAS_PROFILE": profile,
        "SLAS_DATA_ROOT": str(data_root),
        "SLAS_VERSION": version,
        "SLAS_UID": str(uid),
        "SLAS_GID": str(gid),
    }
    for key, value in owned.items():
        if env.get(key) != value:
            env.set(key, value)
            changed.append(key)
    # These a person may have set by hand; the installer fills them only while they are at
    # the template's default.
    settable = {
        "SLAS_REGISTRY": registry,
        "SLAS_TLS_NAMES": tls_names,
        "SLAS_PUBLIC_HOST": public_host,
    }
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

    sec = commands.add_parser("secrets")
    sec.add_argument("--dir", required=True)
    sec.add_argument("--profile", choices=("quickstart", "prod"), required=True)

    lock = commands.add_parser("check-lock")
    lock.add_argument("--lock", required=True)
    lock.add_argument("--profile", choices=("quickstart", "prod"), required=True)

    man = commands.add_parser("check-manifest")
    man.add_argument("--lock", required=True)
    man.add_argument("--manifest", required=True)
    man.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    man.add_argument("--registry", default=None)

    cf = commands.add_parser("compose-files")
    cf.add_argument("--env", required=True)
    cf.add_argument("--profile", choices=("quickstart", "prod"), required=True)
    cf.add_argument("--compose-dir", required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)
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
            images = check_lock(_load_lock(Path(args.lock)), args.profile)
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
