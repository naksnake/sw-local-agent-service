"""`slas backup now|status|restore|drill` (CLAUDE.md §3, §8.4; ADR-0012). Runs pgBackRest
inside the `backup-runner` container through `docker compose exec` (argv only, ADR-0007's
transport) and prints sentences. `drill` performs the restore drill and records the RTO
under `Backups/drills/`.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from slas_cli.doctor.host import Host
from slas_deploy.pgbackrest import BackupError, BackupRunner, RestoreDrill, compose_steps
from slas_hal.hal import CommandResult as HalResult

EXIT_OK = 0
EXIT_PROBLEMS = 1


class HostRunner:
    """The doctor's read-only `Host.run` as a `ProcessRunner` for the backup commands."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        stdin: str | None = None,
        timeout_s: int = 600,
    ) -> HalResult:
        result = self.host.run(list(argv), timeout_s=float(timeout_s))
        return HalResult(
            exit_code=result.returncode if result.returncode is not None else 127,
            stdout=result.stdout,
            stderr=result.stderr,
        )


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def compose_argv(compose_files: Sequence[str], env_file: str) -> list[str]:
    argv = ["docker", "compose", "--project-name", "slas"]
    for path in compose_files:
        argv += ["-f", path]
    argv += ["--env-file", env_file]
    return argv


def run_backup(args: argparse.Namespace, host: Host, out: TextIO) -> int:
    compose = compose_argv(args.compose_file, f"{args.data_root}/.env")
    backups = BackupRunner(HostRunner(host), prefix=[*compose, "exec", "-T", "backup-runner"])
    try:
        if args.backup_command == "now":
            out.write(backups.backup(args.type) + "\n")
            return EXIT_OK
        if args.backup_command == "status":
            infos = backups.info()
            if not infos:
                out.write(
                    "No backup is in the repository yet. The first full backup runs at the "
                    "scheduled hour, or now with `slas backup now --type full`.\n"
                )
                return EXIT_PROBLEMS
            noun = "backup" if len(infos) == 1 else "backups"
            out.write(f"{len(infos)} {noun} in the locked repository:\n")
            for info in infos[-10:]:
                out.write(f"  {info.sentence()}\n")
            out.write(backups.check() + "\n")
            return EXIT_OK
        target = _parse_time(args.to) if getattr(args, "to", None) else None
        if args.backup_command == "restore":
            if not args.yes:
                out.write(
                    "A restore replaces the database with the backup"
                    + (f" as of {target:%Y-%m-%d %H:%M %Z}" if target else "")
                    + ". Stop the platform's writers first (docs/runbooks/restore-drill.md) "
                    "and run again with --yes.\n"
                )
                return EXIT_PROBLEMS
            out.write(backups.restore(target_time=target) + "\n")
            return EXIT_OK
        drill = RestoreDrill(
            backups,
            HostRunner(host),
            compose_steps(compose),
            clock=SystemClock(),
            data_root=Path(args.data_root),
            environment=args.environment,
        )
        record = drill.run(target_time=target)
        out.write(record.sentence() + "\n")
        for phase in record.phases:
            mark = "ok  " if phase.ok else "FAIL"
            note = f" — {phase.note}" if phase.note else ""
            out.write(f"  {mark} {phase.name}: {phase.seconds:.0f} s{note}\n")
        out.write(f"Recorded under {args.data_root}/Backups/drills/.\n")
        return EXIT_OK if record.ok else EXIT_PROBLEMS
    except BackupError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def add_backup_parser(commands: argparse._SubParsersAction, default_data_root: str) -> None:  # type: ignore[type-arg]
    backup = commands.add_parser(
        "backup",
        help="Back up now, list backups, restore to a point in time, or run the restore drill.",
        description="pgBackRest inside the backup-runner container; the repository is under "
        "object lock, so nothing here can delete a backup early.",
    )
    backup.add_argument("--data-root", default=default_data_root)
    backup.add_argument(
        "--compose-file",
        action="append",
        default=None,
        help="Compose files in order (default: compose/docker-compose.yml and prod.override.yml).",
    )
    sub = backup.add_subparsers(dest="backup_command", metavar="<action>")
    now = sub.add_parser("now", help="Take a backup now.")
    now.add_argument("--type", choices=("full", "diff", "incr"), default="diff")
    sub.add_parser("status", help="List the backups and check the archive.")
    restore = sub.add_parser("restore", help="Restore the database (stop the writers first).")
    restore.add_argument("--to", help="Point in time, ISO 8601 (default: the latest backup).")
    restore.add_argument("--yes", action="store_true", help="Confirm the restore.")
    drill = sub.add_parser("drill", help="Run the restore drill and record the RTO.")
    drill.add_argument("--to", help="Point in time to restore to (default: latest).")
    drill.add_argument(
        "--environment",
        default="reference host",
        help="Where the drill runs, recorded with the RTO (reference host, staging, rehearsal).",
    )
