"""A deterministic YAML emitter for the files this package renders (Prometheus, Alertmanager,
Grafana provisioning). PyYAML is not an approved dependency; this writes the subset those
tools read — mappings, sequences, scalars — and quotes every string that is not a plain
identifier with JSON quoting, which YAML 1.2 accepts verbatim. Never a parser: files are
rendered from code and kept in step by tests, not read back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

_PLAIN: Final = re.compile(r"^[A-Za-z0-9_/][A-Za-z0-9_./:-]*$")
_RESERVED: Final = frozenset(
    {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "~", "True", "False", "Null"}
)


def scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    text = str(value)
    if _PLAIN.match(text) and text not in _RESERVED and not _looks_numeric(text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def dump(value: object, *, indent: int = 0) -> str:
    """Render `value` as YAML lines (no document marker)."""
    return "\n".join(_lines(value, indent)) + "\n"


def _lines(value: object, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, Mapping):
        if not value:
            return [f"{pad}{{}}"]
        out: list[str] = []
        for key, item in value.items():
            key_text = scalar(str(key))
            if (isinstance(item, Mapping) and item) or (
                isinstance(item, Sequence) and not isinstance(item, str | bytes) and item
            ):
                out.append(f"{pad}{key_text}:")
                out.extend(_lines(item, indent + 2))
            else:
                out.append(f"{pad}{key_text}: {_inline(item)}")
        return out
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        if not value:
            return [f"{pad}[]"]
        out = []
        for item in value:
            if (isinstance(item, Mapping) and item) or (
                isinstance(item, Sequence) and not isinstance(item, str | bytes) and item
            ):
                nested = _lines(item, indent + 2)
                out.append(f"{pad}- {nested[0].lstrip()}")
                out.extend(nested[1:])
            else:
                out.append(f"{pad}- {_inline(item)}")
        return out
    return [f"{pad}{scalar(value)}"]


def _inline(value: object) -> str:
    if isinstance(value, Mapping):
        return "{}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return "[]"
    return scalar(value)
