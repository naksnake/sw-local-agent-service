"""COLLECT: normalise what every step produced into one log bundle on the ticket (§5.4).

Phase 2 writes the stdout and stderr of every step, fenced per step, under the ticket's
`logs/` directory. Console (SOL) streams and screenshots join in P4 and P7.
"""

from __future__ import annotations

from pathlib import Path

from slas_schemas.envfile import write_atomic
from slas_schemas.ticket import LogBundle, Ticket


def _fence(step_id: str, title: str) -> str:
    return f"--- step {step_id}: {title} ---\n"


def collect_logs(ticket: Ticket, logs_dir: Path) -> LogBundle:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for record in ticket.steps:
        if record.observation is None:
            continue
        stdout_parts.append(_fence(record.step_id, record.title) + record.observation.stdout)
        stderr_parts.append(_fence(record.step_id, record.title) + record.observation.stderr)
    stdout_text = "".join(stdout_parts)
    stderr_text = "".join(stderr_parts)
    stdout_path = logs_dir / "stdout.log"
    stderr_path = logs_dir / "stderr.log"
    write_atomic(stdout_path, stdout_text, mode=0o600)
    write_atomic(stderr_path, stderr_text, mode=0o600)
    return LogBundle(
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        line_counts={
            "stdout": stdout_text.count("\n"),
            "stderr": stderr_text.count("\n"),
        },
    )
