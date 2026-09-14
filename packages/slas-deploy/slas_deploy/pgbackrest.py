"""Backups for the prod profile (CLAUDE.md §8.4, ADR-0012): pgBackRest with point-in-time
recovery to a MinIO repository under object lock, the same object lock on run artifacts, and
the backup runner and restore drill that drive it.

    config/pgbackrest.conf         stanza slas, repository on MinIO (S3), retention, async archive
    config/postgres/prod.conf      archive_mode on, archive_command → pgbackrest, wal_level replica
    deploy/prod/minio-init.sh      buckets with object lock: backups (compliance), artifacts
                                   (governance); nothing can be deleted before its retention
    deploy/prod/backup-runner.sh   the schedule: full weekly, differential daily, check hourly

`BackupRunner` builds every pgBackRest argv (no shell) and `RestoreDrill` measures a full
restore to a point in time, phase by phase, writing the record under `Backups/drills/`.
"""

from __future__ import annotations

# ruff: noqa: E501 — embedded shell scripts and compose headers read better on one line
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field

from slas_hal.drivers.process import ProcessRunner
from slas_hal.hal import CommandResult
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage

STANZA: Final = "slas"
BACKUP_BUCKET: Final = "slas-backups"
ARTIFACT_BUCKET: Final = "slas-artifacts"
PG_PATH: Final = "/var/lib/postgresql/data"


def pgbackrest_conf(*, retention_full: int = 4, retention_diff: int = 14) -> str:
    return (
        "# pgBackRest for SW Local Agent Service, prod profile (CLAUDE.md §8.4, ADR-0012).\n"
        "# Rendered from slas_deploy.pgbackrest; a unit test keeps file and code in step.\n"
        "# The repository is MinIO on the backend network with object lock (compliance mode):\n"
        "# a backup cannot be deleted before its retention, by anyone. Credentials come from\n"
        "# Docker secret files, never from this file.\n"
        "[global]\n"
        "repo1-type=s3\n"
        "repo1-s3-endpoint=minio:9000\n"
        f"repo1-s3-bucket={BACKUP_BUCKET}\n"
        "repo1-s3-region=us-east-1\n"
        "repo1-s3-uri-style=path\n"
        "repo1-s3-verify-tls=n\n"
        "repo1-s3-key-file=/run/secrets/pgbackrest_s3_key\n"
        "repo1-s3-key-secret-file=/run/secrets/pgbackrest_s3_secret\n"
        "repo1-path=/pgbackrest\n"
        f"repo1-retention-full={retention_full}\n"
        f"repo1-retention-diff={retention_diff}\n"
        "repo1-retention-archive-type=full\n"
        "repo1-cipher-type=none\n"
        "process-max=4\n"
        "compress-type=zst\n"
        "compress-level=3\n"
        "archive-async=y\n"
        "spool-path=/var/spool/pgbackrest\n"
        "log-level-console=info\n"
        "log-level-file=off\n"
        "start-fast=y\n"
        "delta=y\n"
        "\n"
        f"[{STANZA}]\n"
        f"pg1-path={PG_PATH}\n"
        "pg1-port=5432\n"
        "pg1-socket-path=/var/run/postgresql\n"
    )


def postgres_prod_conf() -> str:
    return (
        "# PostgreSQL settings the prod profile adds (ADR-0012): continuous archiving through\n"
        "# pgBackRest for point-in-time recovery. Rendered from slas_deploy.pgbackrest; a unit\n"
        "# test keeps file and code in step. Everything else stays at the image's defaults.\n"
        "listen_addresses = '*'\n"
        "max_connections = 200\n"
        "wal_level = replica\n"
        "archive_mode = on\n"
        f"archive_command = 'pgbackrest --stanza={STANZA} archive-push %p'\n"
        "archive_timeout = 300\n"
        "max_wal_senders = 3\n"
        "wal_keep_size = 512MB\n"
        "checkpoint_timeout = 15min\n"
        "log_destination = 'stderr'\n"
        "log_line_prefix = '%m [%p] %q%u@%d '\n"
        "log_min_duration_statement = 2000\n"
        "shared_preload_libraries = 'pg_stat_statements'\n"
    )


def minio_init_script() -> str:
    """Buckets with object lock. Compliance mode for backups: not even root deletes early.
    Governance for run artifacts: an administrator with the bypass permission can, and the
    act is logged. Idempotent: `mc mb --ignore-existing` and `retention set` are safe twice."""
    return (
        "#!/bin/sh\n"
        "# MinIO buckets for the prod profile (CLAUDE.md §8.4 object-lock, ADR-0012). Rendered\n"
        "# from slas_deploy.pgbackrest; a unit test keeps file and code in step. Runs once per\n"
        "# `docker compose up` as the minio-init service; every step is idempotent.\n"
        "set -eu\n"
        'ROOT_PASSWORD="$(cat /run/secrets/minio_root_password)"\n'
        'BACKUP_KEY="$(cat /run/secrets/pgbackrest_s3_key)"\n'
        'BACKUP_SECRET="$(cat /run/secrets/pgbackrest_s3_secret)"\n'
        'mc alias set slas http://minio:9000 "$MINIO_ROOT_USER" "$ROOT_PASSWORD" >/dev/null\n'
        f"mc mb --ignore-existing --with-lock slas/{BACKUP_BUCKET}\n"
        f"mc mb --ignore-existing --with-lock slas/{ARTIFACT_BUCKET}\n"
        f'mc retention set --default compliance "${{BACKUP_RETENTION_DAYS}}d" slas/{BACKUP_BUCKET}\n'
        f'mc retention set --default governance "${{ARTIFACT_RETENTION_DAYS}}d" slas/{ARTIFACT_BUCKET}\n'
        f"mc version enable slas/{BACKUP_BUCKET}\n"
        f"mc version enable slas/{ARTIFACT_BUCKET}\n"
        "# The backup user may write and read the backup bucket and nothing else.\n"
        'mc admin user add slas "$BACKUP_KEY" "$BACKUP_SECRET" >/dev/null 2>&1 || true\n'
        "cat > /tmp/backup-policy.json <<'EOF'\n"
        "{\n"
        '  "Version": "2012-10-17",\n'
        '  "Statement": [\n'
        '    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],\n'
        f'     "Resource": ["arn:aws:s3:::{BACKUP_BUCKET}"]}},\n'
        '    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectRetention"],\n'
        f'     "Resource": ["arn:aws:s3:::{BACKUP_BUCKET}/*"]}}\n'
        "  ]\n"
        "}\n"
        "EOF\n"
        "mc admin policy create slas slas-backup-writer /tmp/backup-policy.json >/dev/null 2>&1 || true\n"
        'mc admin policy attach slas slas-backup-writer --user "$BACKUP_KEY" >/dev/null 2>&1 || true\n'
        f'echo "Object lock is on: {BACKUP_BUCKET} keeps backups ${{BACKUP_RETENTION_DAYS}} days '
        f'(compliance), {ARTIFACT_BUCKET} keeps run artifacts ${{ARTIFACT_RETENTION_DAYS}} days (governance)."\n'
    )


def backup_runner_script() -> str:
    return (
        "#!/bin/sh\n"
        "# The backup schedule for the prod profile (CLAUDE.md §8.4, ADR-0012). Rendered from\n"
        "# slas_deploy.pgbackrest; a unit test keeps file and code in step. One loop, no cron\n"
        "# daemon: a full backup when the day matches BACKUP_FULL_CRON's weekday, a differential\n"
        "# every other day at BACKUP_DIFF_CRON's hour, `pgbackrest check` every hour, and a Qdrant\n"
        "# snapshot next to every backup. Every run writes one line to Backups/backup-journal.jsonl.\n"
        "set -eu\n"
        'STANZA="${PGBACKREST_STANZA:-slas}"\n'
        'JOURNAL="${SLAS_DATA_ROOT:-/data}/Backups/backup-journal.jsonl"\n'
        'FULL_DAY="${BACKUP_FULL_CRON:-0}"     # weekday 0-6 (Sunday = 0)\n'
        'AT_HOUR="${BACKUP_DIFF_CRON:-2}"       # hour of day, 0-23\n'
        'mkdir -p "$(dirname "$JOURNAL")"\n'
        "record() {\n"
        '  printf \'{"at":"%s","kind":"%s","ok":%s,"seconds":%s}\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" >> "$JOURNAL"\n'
        "}\n"
        "run_backup() {\n"
        "  start=$(date +%s)\n"
        '  if pgbackrest --stanza="$STANZA" --type="$1" backup; then ok=true; else ok=false; fi\n'
        '  record "backup-$1" "$ok" "$(( $(date +%s) - start ))"\n'
        '  if [ -d "${SLAS_DATA_ROOT:-/data}/qdrant" ]; then\n'
        '    tar -C "${SLAS_DATA_ROOT:-/data}" -czf "${SLAS_DATA_ROOT:-/data}/Backups/qdrant/qdrant-$(date -u +%Y%m%dT%H%M%SZ).tgz" qdrant 2>/dev/null || true\n'
        "  fi\n"
        "}\n"
        'mkdir -p "${SLAS_DATA_ROOT:-/data}/Backups/qdrant"\n'
        'pgbackrest --stanza="$STANZA" stanza-create 2>/dev/null || true\n'
        'last_backup_day=""\n'
        "while true; do\n"
        "  now_hour=$(date -u +%H); now_day=$(date -u +%Y-%m-%d); weekday=$(date -u +%w)\n"
        '  if [ "$now_hour" -eq "$AT_HOUR" ] && [ "$now_day" != "$last_backup_day" ]; then\n'
        '    if [ "$weekday" -eq "$FULL_DAY" ]; then run_backup full; else run_backup diff; fi\n'
        '    last_backup_day="$now_day"\n'
        "  fi\n"
        "  start=$(date +%s)\n"
        '  if pgbackrest --stanza="$STANZA" check >/dev/null 2>&1; then ok=true; else ok=false; fi\n'
        '  record check "$ok" "$(( $(date +%s) - start ))"\n'
        "  sleep 3600\n"
        "done\n"
    )


# --- the runner and the drill --------------------------------------------------------------------


class BackupError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class BackupInfo(SlasModel):
    label: str
    kind: Literal["full", "diff", "incr"]
    started_at: str
    stopped_at: str
    size_bytes: int = 0

    def sentence(self) -> str:
        return f"{self.kind} backup {self.label}, finished {self.stopped_at}"


class BackupRunner:
    """pgBackRest by argv (`docker compose exec backup-runner pgbackrest …` on the host,
    plain `pgbackrest` inside the container). Never a shell."""

    def __init__(
        self, runner: ProcessRunner, *, prefix: Sequence[str] = (), stanza: str = STANZA
    ) -> None:
        self.runner = runner
        self.prefix = list(prefix)
        self.stanza = stanza
        self.calls: list[list[str]] = []

    def _run(self, *argv: str, timeout_s: int = 3600) -> CommandResult:
        full = [*self.prefix, "pgbackrest", f"--stanza={self.stanza}", *argv]
        self.calls.append(full)
        return self.runner.run(full, env={}, timeout_s=timeout_s)

    def _require(self, result: CommandResult, what: str) -> CommandResult:
        if result.exit_code != 0:
            raise BackupError(
                ThreePartMessage(
                    f"{what} did not finish.",
                    (
                        result.stderr.strip().splitlines()
                        or [f"pgbackrest exited {result.exit_code}"]
                    )[-1],
                    "Run `slas backup status` for the repository's state, then check the "
                    "backup-runner log (`slas logs backup-runner`).",
                )
            )
        return result

    def backup(self, kind: Literal["full", "diff", "incr"]) -> str:
        self._require(self._run("--type", kind, "backup"), f"The {kind} backup")
        return f"The {kind} backup finished and is in the locked repository."

    def check(self) -> str:
        self._require(self._run("check", timeout_s=300), "The repository check")
        return "The archive and the repository agree; the next restore has what it needs."

    def info(self) -> list[BackupInfo]:
        result = self._require(
            self._run("--output=json", "info", timeout_s=300), "Reading the backups"
        )
        try:
            stanzas = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        backups: list[BackupInfo] = []
        for stanza in stanzas:
            for item in stanza.get("backup", []):
                stamp = item.get("timestamp", {})
                raw_kind = str(item.get("type", "full"))
                kind: Literal["full", "diff", "incr"] = (
                    "diff" if raw_kind == "diff" else "incr" if raw_kind == "incr" else "full"
                )
                backups.append(
                    BackupInfo(
                        label=str(item.get("label", "?")),
                        kind=kind,
                        started_at=_iso(stamp.get("start")),
                        stopped_at=_iso(stamp.get("stop")),
                        size_bytes=int(item.get("info", {}).get("size", 0) or 0),
                    )
                )
        return backups

    def restore(self, *, target_time: datetime | None, delta: bool = True) -> str:
        argv = ["restore"]
        if delta:
            argv.append("--delta")
        if target_time is not None:
            argv += [
                "--type=time",
                f"--target={target_time.strftime('%Y-%m-%d %H:%M:%S%z')}",
                "--target-action=promote",
            ]
        self._require(self._run(*argv, timeout_s=7200), "The restore")
        when = f"to {target_time:%Y-%m-%d %H:%M %Z}" if target_time else "to the latest backup"
        return f"PostgreSQL was restored {when}; it replays the archive and promotes on start."


def _iso(epoch: object) -> str:
    try:
        return datetime.fromtimestamp(int(str(epoch)), tz=None).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "?"


class DrillPhase(SlasModel):
    name: str
    seconds: float = Field(ge=0)
    ok: bool
    note: str = ""


class DrillRecord(SlasModel):
    """One restore drill: the phases, the recovery time, whether the platform came back."""

    started_at: datetime
    target_time: datetime | None
    phases: list[DrillPhase] = Field(default_factory=list)
    rto_seconds: float = Field(default=0, ge=0)
    ok: bool = False
    #: Where the drill ran: the reference host, a staging host, or a rehearsal against fakes.
    environment: str = "reference host"

    def sentence(self) -> str:
        minutes = self.rto_seconds / 60
        state = "came back" if self.ok else "did not come back"
        when = f" to {self.target_time:%Y-%m-%d %H:%M}" if self.target_time else ""
        return (
            f"Restore drill on {self.started_at:%Y-%m-%d} ({self.environment}): the platform "
            f"{state}{when} in {minutes:.1f} minutes across {len(self.phases)} phases."
        )


class Steps(SlasModel):
    """The commands around the restore, as argv: stop the writers, start the database, verify."""

    stop_writers: list[str]
    stop_postgres: list[str]
    start_postgres: list[str]
    wait_ready: list[str]
    verify: list[str]
    start_writers: list[str]


def compose_steps(compose_argv: Sequence[str]) -> Steps:
    """The drill through `docker compose` on the platform host."""
    dc = list(compose_argv)
    writers = [
        "api",
        "agent-core-orchestrator",
        "git-broker",
        "validation-executor",
        "factory-executor",
    ]
    return Steps(
        stop_writers=[*dc, "stop", *writers],
        stop_postgres=[*dc, "stop", "postgres"],
        start_postgres=[*dc, "start", "postgres"],
        wait_ready=[*dc, "exec", "-T", "postgres", "pg_isready", "-U", "${POSTGRES_USER}"],
        verify=[*dc, "exec", "-T", "api", "slas-api", "verify-restore"],
        start_writers=[*dc, "start", *writers],
    )


class RestoreDrill:
    def __init__(
        self,
        backups: BackupRunner,
        runner: ProcessRunner,
        steps: Steps,
        *,
        clock: Any,
        data_root: Path,
        environment: str = "reference host",
    ) -> None:
        self.backups = backups
        self.runner = runner
        self.steps = steps
        self.clock = clock
        self.data_root = data_root
        self.environment = environment

    def _phase(
        self, record: DrillRecord, name: str, argv: Sequence[str], *, timeout_s: int = 1800
    ) -> bool:
        started = self.clock.now()
        result = self.runner.run(list(argv), env={}, timeout_s=timeout_s)
        seconds = (self.clock.now() - started).total_seconds()
        note = (
            "" if result.exit_code == 0 else (result.stderr.strip().splitlines() or ["failed"])[-1]
        )
        record.phases.append(
            DrillPhase(name=name, seconds=seconds, ok=result.exit_code == 0, note=note)
        )
        return result.exit_code == 0

    def run(self, *, target_time: datetime | None) -> DrillRecord:
        started = self.clock.now()
        record = DrillRecord(
            started_at=started, target_time=target_time, environment=self.environment
        )
        ok = self._phase(record, "stop writers", self.steps.stop_writers)
        ok = ok and self._phase(record, "stop postgres", self.steps.stop_postgres)
        if ok:
            phase_start = self.clock.now()
            try:
                note = self.backups.restore(target_time=target_time)
                restore_ok = True
            except BackupError as exc:
                note = exc.message.likely_cause
                restore_ok = False
            record.phases.append(
                DrillPhase(
                    name="pgbackrest restore",
                    seconds=(self.clock.now() - phase_start).total_seconds(),
                    ok=restore_ok,
                    note=note,
                )
            )
            ok = restore_ok
        ok = ok and self._phase(record, "start postgres", self.steps.start_postgres)
        ok = ok and self._phase(record, "wait until ready", self.steps.wait_ready, timeout_s=900)
        ok = ok and self._phase(record, "verify the platform's data", self.steps.verify)
        ok = self._phase(record, "start writers", self.steps.start_writers) and ok
        record.ok = ok
        record.rto_seconds = (self.clock.now() - started).total_seconds()
        self._write(record)
        return record

    def _write(self, record: DrillRecord) -> Path:
        path = self.data_root / "Backups" / "drills" / f"{record.started_at:%Y%m%dT%H%M%SZ}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, record.model_dump_json(indent=2) + "\n", mode=0o644)
        return path
