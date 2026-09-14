"""Read and write `.env` files without losing anything (CLAUDE.md §3, INV-9).

One writer serves the installer and Admin → Settings: comments, blank lines, ordering and
keys we do not know about are preserved byte for byte, only the targeted lines change, and
every write is atomic and keeps the requested file mode. Stdlib only, so the host CLI can
use it without a virtualenv.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BARE_VALUE = re.compile(r"^[A-Za-z0-9_./:@,+-]*$")


@dataclass(frozen=True, slots=True)
class Line:
    """One physical line. `key` is None for comments and blank lines."""

    raw: str
    key: str | None = None
    value: str | None = None


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == "'":
            return inner
        out: list[str] = []
        escaped = False
        for char in inner:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                out.append(char)
        return "".join(out)
    # Unquoted values may carry a trailing comment: KEY=value # note
    head, _, _ = text.partition(" #")
    return head.strip()


def _quote(value: str) -> str:
    if _BARE_VALUE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_line(raw: str) -> Line:
    match = _ASSIGNMENT.match(raw)
    if match is None or raw.lstrip().startswith("#"):
        return Line(raw)
    return Line(raw, match.group("key"), _unquote(match.group("value")))


class EnvFile:
    """A `.env` file kept as lines so that a rewrite changes only what it must."""

    def __init__(self, lines: list[Line] | None = None) -> None:
        self._lines: list[Line] = list(lines or [])

    @classmethod
    def parse(cls, text: str) -> EnvFile:
        parts = text.split("\n")
        if parts and parts[-1] == "":
            parts.pop()
        return cls([parse_line(raw) for raw in parts])

    @property
    def lines(self) -> tuple[Line, ...]:
        return tuple(self._lines)

    def keys(self) -> list[str]:
        seen: list[str] = []
        for line in self._lines:
            if line.key is not None and line.key not in seen:
                seen.append(line.key)
        return seen

    def __contains__(self, key: object) -> bool:
        return any(line.key == key for line in self._lines)

    def get(self, key: str) -> str | None:
        """The value of the last assignment of `key`, like the shells do."""
        value: str | None = None
        for line in self._lines:
            if line.key == key:
                value = line.value
        return value

    def set(self, key: str, value: str, *, under_marker: str | None = None) -> bool:
        """Set `key`, replacing the last assignment in place or appending.

        With `under_marker`, a missing key is appended after the marker comment (and the
        assignments already grouped under it); the marker itself is created when absent.
        Returns True when the text changed.
        """
        if not _KEY.match(key):
            raise ValueError(f"{key!r} is not a valid .env key")
        rendered = f"{key}={_quote(value)}"
        new_line = Line(rendered, key, value)
        for index in range(len(self._lines) - 1, -1, -1):
            if self._lines[index].key == key:
                if self._lines[index].raw == rendered:
                    return False
                self._lines[index] = new_line
                return True
        if under_marker is None:
            self._lines.append(new_line)
            return True
        marker_index = next(
            (
                i
                for i, line in enumerate(self._lines)
                if line.key is None and line.raw == under_marker
            ),
            None,
        )
        if marker_index is None:
            if self._lines and self._lines[-1].raw.strip():
                self._lines.append(Line(""))
            self._lines.append(Line(under_marker))
            self._lines.append(new_line)
            return True
        insert_at = marker_index + 1
        while insert_at < len(self._lines) and self._lines[insert_at].key is not None:
            insert_at += 1
        self._lines.insert(insert_at, new_line)
        return True

    def remove(self, key: str) -> bool:
        before = len(self._lines)
        self._lines = [line for line in self._lines if line.key != key]
        return len(self._lines) != before

    def render(self) -> str:
        if not self._lines:
            return ""
        return "\n".join(line.raw for line in self._lines) + "\n"


def read_env(path: Path) -> EnvFile:
    return EnvFile.parse(path.read_text(encoding="utf-8"))


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Write `text` to `path` so that a crash leaves either the old or the new file."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def generate_secret(nbytes: int = 32) -> str:
    """A URL-safe random secret; 32 bytes gives 43 characters."""
    return secrets.token_urlsafe(nbytes)
