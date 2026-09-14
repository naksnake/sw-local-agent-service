"""`{{ name }}` binding and the small condition language skills use.

Templates resolve against inputs (`{{ user }}` or `{{ inputs.user }}`), variables set by
`set`, and step outputs (`{{ steps.sel.count }}`). A reference to a step output that is
not known yet is kept as-is at compile time and resolved by the runner after that step.
Conditions are one comparison (`==`, `!=`, `<`, `<=`, `>`, `>=`) between two operands, or a
single operand tested for truthiness. There is no eval and no function call (INV-12).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from slas_schemas.errors import ThreePartMessage

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}")
_COMPARISON = re.compile(r"^(.*?)\s*(==|!=|<=|>=|<|>)\s*(.*)$")
_LATE_PREFIXES = ("steps.",)


class TemplateError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def references(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def _lookup(path: str, context: Mapping[str, Any]) -> tuple[bool, Any]:
    parts = path.split(".")
    if parts[0] == "inputs" and len(parts) > 1:
        parts = parts[1:]
    current: Any = context
    for part in parts:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def render(text: str, context: Mapping[str, Any], *, source: str = "the skill") -> Any:
    """Render one string. A string that is exactly one placeholder keeps the value's type."""
    whole = PLACEHOLDER.fullmatch(text.strip())
    if whole:
        found, value = _lookup(whole.group(1), context)
        if found:
            return value
        if whole.group(1).startswith(_LATE_PREFIXES):
            return text
        raise _unknown(whole.group(1), source)

    def substitute(match: re.Match[str]) -> str:
        found, value = _lookup(match.group(1), context)
        if found:
            return str(value)
        if match.group(1).startswith(_LATE_PREFIXES):
            return match.group(0)
        raise _unknown(match.group(1), source)

    return PLACEHOLDER.sub(substitute, text)


def _unknown(name: str, source: str) -> TemplateError:
    return TemplateError(
        ThreePartMessage(
            f"{source} refers to {{{{ {name} }}}}, which is not an input or a variable.",
            "Inputs are declared under `inputs:`; variables come from `set` steps; step outputs "
            "are written `steps.<id>.<field>`.",
            "Declare the input, or fix the name.",
        )
    )


def render_value(value: Any, context: Mapping[str, Any], *, source: str = "the skill") -> Any:
    if isinstance(value, str):
        return render(value, context, source=source)
    if isinstance(value, list):
        return [render_value(item, context, source=source) for item in value]
    if isinstance(value, Mapping):
        return {key: render_value(item, context, source=source) for key, item in value.items()}
    return value


def has_late_reference(value: Any) -> bool:
    if isinstance(value, str):
        return any(ref.startswith(_LATE_PREFIXES) for ref in references(value))
    if isinstance(value, list):
        return any(has_late_reference(item) for item in value)
    if isinstance(value, Mapping):
        return any(has_late_reference(item) for item in value.values())
    return False


def _operand(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "none", "null", "no")
    return bool(value)


def evaluate(condition: str, context: Mapping[str, Any], *, source: str = "the skill") -> bool:
    rendered = render(condition, context, source=source)
    if not isinstance(rendered, str):
        return truthy(rendered)
    match = _COMPARISON.match(rendered.strip())
    if match is None:
        return truthy(_operand(rendered))
    left, op, right = _operand(match.group(1)), match.group(2), _operand(match.group(3))
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    try:
        if op == "<":
            return bool(left < right)
        if op == "<=":
            return bool(left <= right)
        if op == ">":
            return bool(left > right)
        return bool(left >= right)
    except TypeError as exc:
        raise TemplateError(
            ThreePartMessage(
                f"{source} compares values that cannot be ordered: {rendered.strip()}.",
                "Ordering comparisons need numbers on both sides.",
                "Use == or != for text, or make both sides numbers.",
            )
        ) from exc
