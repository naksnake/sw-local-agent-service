"""Screenshot retention (P10): how long the platform and a station keep the PNGs every GUI
step produces. A policy, a deterministic prune, and a sentence about what happened.

    keep_days          screenshots of finished jobs older than this are deleted
    keep_failed_days   jobs that failed or are held keep theirs longer
    max_per_job        above this many per job, the oldest go first; a step's failure shot,
                       the first and the last screenshot of a job are always kept
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import Field

from slas_schemas.common import SlasModel


class RetentionPolicy(SlasModel):
    keep_days: int = Field(default=30, ge=1, le=3650)
    keep_failed_days: int = Field(default=180, ge=1, le=3650)
    max_per_job: int = Field(default=400, ge=10, le=100_000)

    def sentence(self) -> str:
        return (
            f"Screenshots are kept {self.keep_days} days ({self.keep_failed_days} days for "
            f"failed or held jobs), at most {self.max_per_job} per job."
        )


class PruneReport(SlasModel):
    scanned: int = 0
    deleted: int = 0
    kept: int = 0
    jobs: int = 0

    def sentence(self) -> str:
        return (
            f"Looked at {self.scanned} screenshots in {self.jobs} "
            f"{'job' if self.jobs == 1 else 'jobs'}: deleted {self.deleted}, kept {self.kept}."
        )


def _age_days(path: Path, now: datetime) -> float:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return (now - modified) / timedelta(days=1)


def prune_screenshots(
    root: Path,
    policy: RetentionPolicy,
    *,
    now: datetime,
    failed_jobs: Iterable[str] = (),
) -> PruneReport:
    """Prune every `*.png` under `root`, grouped by the directory it sits in. A group whose
    directory (or its parent, for `<job>/screens/`) is named in `failed_jobs` keeps its
    screenshots `keep_failed_days`."""
    failed = set(failed_jobs)
    report = PruneReport()
    if not root.is_dir():
        return report
    groups: dict[Path, list[Path]] = {}
    for path in root.rglob("*.png"):
        groups.setdefault(path.parent, []).append(path)
    report.jobs = len(groups)
    for directory, files in groups.items():
        job_names = {directory.name, directory.parent.name}
        limit_days = policy.keep_failed_days if job_names & failed else policy.keep_days
        files.sort(key=lambda p: (p.stat().st_mtime, p.name))
        report.scanned += len(files)
        protected = {files[0], files[-1], *(f for f in files if "failure" in f.name)}
        remaining: list[Path] = []
        for path in files:
            if _age_days(path, now) > limit_days and path not in protected:
                os.unlink(path)
                report.deleted += 1
            else:
                remaining.append(path)
        if len(remaining) > policy.max_per_job:
            excess = len(remaining) - policy.max_per_job
            for path in [p for p in remaining if p not in protected][:excess]:
                os.unlink(path)
                remaining.remove(path)
                report.deleted += 1
        report.kept += len(remaining)
    return report
