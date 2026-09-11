"""Reading and writing `${SLAS_DATA_ROOT}/.env` (CLAUDE.md §3, §4.4).

The file holds non-secret settings only: profile, data root, edge port, engine, compose
location, version, log level. Passwords live as compose file secrets under
`${SLAS_DATA_ROOT}/secrets/` (ADR-0004). install.sh writes the file once; a re-run keeps every
existing value, so the file is never rotated behind the operator's back.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

ENV_FILE_MODE = 0o600


def parse_env(text: str) -> dict[str, str]:
    """`KEY=VALUE` per line; blank lines and `#` comments are ignored; quotes are stripped."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] and value[0] == "'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_env(path.read_text(encoding="utf-8"))


def render_env(values: Mapping[str, str], *, header: str = "") -> str:
    """One `KEY=VALUE` per line in insertion order, preceded by an optional comment header."""
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}" if line else "#" for line in header.splitlines())
    for key, value in values.items():
        if any(ch in value for ch in " #\"'\\"):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def write_private_file(path: Path, text: str, *, mode: int = ENV_FILE_MODE) -> None:
    """Write atomically with the final mode set before the file becomes visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
