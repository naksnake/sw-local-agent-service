"""Station state backup (CLAUDE.md §10.3 BACKUP): what the runner collected, written under
`Backups/stations/<station>/<ticket>/` with a manifest of hashes, attached as an export."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from slas_schemas.envfile import write_atomic
from slas_schemas.ticket import Export
from slas_station_runner.protocol import StateSnapshot


def backup_dir(data_root: Path, station: str, ticket_id: str) -> Path:
    return data_root / "Backups" / "stations" / station / ticket_id


def write_backup(
    data_root: Path, *, station: str, ticket_id: str, snapshot: StateSnapshot, now: datetime
) -> Export:
    root = backup_dir(data_root, station, ticket_id)
    files_dir = root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "station": station,
        "ticket": ticket_id,
        "taken_at": snapshot.taken_at.isoformat(),
        "written_at": now.isoformat(),
        "versions": snapshot.versions,
        "files": {},
    }
    entries: dict[str, dict[str, object]] = {}
    for name, content in snapshot.files.items():
        safe = name.strip("/").replace("/", "__") or "file"
        target = files_dir / safe
        write_atomic(target, content, mode=0o600)
        entries[name] = {
            "stored_as": f"files/{safe}",
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "bytes": len(content.encode("utf-8")),
        }
    manifest["files"] = entries
    write_atomic(root / "versions.json", json.dumps(snapshot.versions, indent=2) + "\n", mode=0o600)
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    write_atomic(root / "manifest.json", text, mode=0o600)
    return Export(
        kind="backup", path=str(root), sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def backup_sentence(snapshot: StateSnapshot, export: Export) -> str:
    versions = ", ".join(f"{k} {v}" for k, v in sorted(snapshot.versions.items()))
    return (
        f"Backed up {len(snapshot.files)} files and the versions ({versions or 'none reported'}) "
        f"to {export.path}."
    )
