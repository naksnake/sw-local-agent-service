"""ZIP export of a project — always available, never includes `.git` (§10.1 EXPORT)."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Final

from slas_schemas.ticket import Export

#: Fixed timestamps so the same tree always zips to the same bytes (reproducible, §1.2).
_EPOCH: Final = (2026, 1, 1, 0, 0, 0)
_SKIP_DIRS: Final = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "target",
        ".venv",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
    }
)


def zip_project(project_dir: Path, out_path: Path) -> Export:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p
        for p in project_dir.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not (_SKIP_DIRS & set(p.relative_to(project_dir).parts))
    )
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(str(path.relative_to(project_dir)), date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0755 for scripts the agent made executable (a #! line), 0644 for the rest; a
            # flat 0644 left `./templog.py` with "Permission denied" after unzipping.
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = mode << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return Export(kind="zip", path=str(out_path), sha256=digest)
