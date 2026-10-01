"""A small JSON Schema checker for the answers `guided_json` was meant to constrain.

Over the wire (`POST /v1/generate`) the caller sends a JSON Schema, not a Pydantic class, so
the gateway needs to check a model's answer against a schema it did not generate itself. This
covers the subset Pydantic emits for its own models (draft 2020-12): `$ref` into `$defs`,
`type`, `enum`, `const`, `properties` / `required` / `additionalProperties`, `items` /
`prefixItems` with the length bounds, the numeric bounds, `minLength` / `maxLength` /
`pattern`, and `anyOf` / `oneOf` / `allOf`. Anything else (`format`, `not`, remote `$ref`)
is not checked here: tier 0 (constrained decoding) already enforced it in vLLM, and this is
the deterministic second look that stops a partially valid object from passing (CLAUDE.md
§11). No third-party validator is added (§0.3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

#: Deeper than any plan or edit set; stops a self-referencing schema from looping.
MAX_DEPTH: Final = 64

_TYPE_NAMES: Final = ("object", "array", "string", "integer", "number", "boolean", "null")


class SchemaMismatchError(ValueError):
    """`str(error)` is one plain fragment: "edits.0.path: is missing"."""

    def __init__(self, path: str, problem: str) -> None:
        self.path = path
        self.problem = problem
        super().__init__(f"{path}: {problem}" if path else problem)


def _is_type(value: object, name: str) -> bool:
    if name == "object":
        return isinstance(value, Mapping)
    if name == "array":
        return isinstance(value, list | tuple)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, int | float)
    raise SchemaMismatchError("", f"the schema names an unknown type {name!r}")


def _describe(value: object) -> str:
    for name in _TYPE_NAMES:
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if _is_type(value, name):
            return "an integer" if name == "integer" else f"a {name}" if name != "null" else "null"
    return type(value).__name__  # pragma: no cover — every JSON value has a type above


def _join(path: str, part: str | int) -> str:
    return f"{path}.{part}" if path else str(part)


def _resolve_ref(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaMismatchError(
            "", f"the schema uses a reference this checker cannot follow: {ref}"
        )
    node: Any = root
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        else:
            raise SchemaMismatchError("", f"the schema references {ref}, which does not exist")
    if not isinstance(node, Mapping):
        raise SchemaMismatchError("", f"the schema references {ref}, which is not a schema")
    return node


def _check_number(value: float, schema: Mapping[str, Any], path: str) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise SchemaMismatchError(path, f"must be at least {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise SchemaMismatchError(path, f"must be at most {schema['maximum']}")
    if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
        raise SchemaMismatchError(path, f"must be greater than {schema['exclusiveMinimum']}")
    if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
        raise SchemaMismatchError(path, f"must be less than {schema['exclusiveMaximum']}")


def _check_string(value: str, schema: Mapping[str, Any], path: str) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        noun = "character" if schema["minLength"] == 1 else "characters"
        raise SchemaMismatchError(path, f"must have at least {schema['minLength']} {noun}")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaMismatchError(path, f"must have at most {schema['maxLength']} characters")
    pattern = schema.get("pattern")
    if pattern is not None and re.search(pattern, value) is None:
        raise SchemaMismatchError(path, f"does not match the pattern {pattern}")


def _check_array(
    value: Sequence[Any], schema: Mapping[str, Any], path: str, root: Mapping[str, Any], depth: int
) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        raise SchemaMismatchError(path, f"must have at least {schema['minItems']} items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise SchemaMismatchError(path, f"must have at most {schema['maxItems']} items")
    prefix = schema.get("prefixItems", [])
    for index, item in enumerate(value):
        if index < len(prefix):
            _check(item, prefix[index], _join(path, index), root, depth + 1)
        elif isinstance(schema.get("items"), Mapping):
            _check(item, schema["items"], _join(path, index), root, depth + 1)


def _check_object(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
    root: Mapping[str, Any],
    depth: int,
) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            raise SchemaMismatchError(_join(path, name), "is missing")
    for name, item in value.items():
        if name in properties:
            _check(item, properties[name], _join(path, name), root, depth + 1)
            continue
        extra = schema.get("additionalProperties", True)
        if extra is False:
            raise SchemaMismatchError(_join(path, name), "is not an allowed field")
        if isinstance(extra, Mapping):
            _check(item, extra, _join(path, name), root, depth + 1)


def _check_alternatives(
    value: object,
    options: Sequence[Mapping[str, Any]],
    keyword: str,
    path: str,
    root: Mapping[str, Any],
    depth: int,
) -> None:
    matched = 0
    reasons: list[str] = []
    for option in options:
        try:
            _check(value, option, path, root, depth + 1)
        except SchemaMismatchError as error:
            reasons.append(error.problem if error.path == path else str(error))
        else:
            matched += 1
    if matched == 0:
        raise SchemaMismatchError(path, f"matches none of the allowed forms ({'; '.join(reasons)})")
    if keyword == "oneOf" and matched > 1:
        raise SchemaMismatchError(path, "matches more than one of the allowed forms")


def _check(
    value: object, schema: Mapping[str, Any], path: str, root: Mapping[str, Any], depth: int
) -> None:
    if depth > MAX_DEPTH:
        raise SchemaMismatchError(path, "the schema nests deeper than the checker follows")
    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"], root)
        _check(value, target, path, root, depth + 1)

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_is_type(value, name) for name in names):
            wanted = " or ".join(names)
            raise SchemaMismatchError(path, f"must be {wanted}, not {_describe(value)}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(json.dumps(option) for option in schema["enum"])
        raise SchemaMismatchError(path, f"must be one of {allowed}")
    if "const" in schema and value != schema["const"]:
        raise SchemaMismatchError(path, f"must be {json.dumps(schema['const'])}")

    if isinstance(value, bool) or value is None:
        pass
    elif isinstance(value, int | float):
        _check_number(value, schema, path)
    elif isinstance(value, str):
        _check_string(value, schema, path)
    elif isinstance(value, list | tuple):
        _check_array(value, schema, path, root, depth)
    elif isinstance(value, Mapping):
        _check_object(value, schema, path, root, depth)

    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            _check_alternatives(value, schema[keyword], keyword, path, root, depth)
    for option in schema.get("allOf", []):
        _check(value, option, path, root, depth + 1)


def check(value: object, schema: Mapping[str, Any]) -> None:
    """Raise `SchemaMismatchError` at the first place `value` departs from `schema`."""
    _check(value, schema, "", schema, 0)


def validate_json(text: str, schema: Mapping[str, Any]) -> Any:
    """Parse `text` and check it against `schema`; the parsed value comes back."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise SchemaMismatchError(
            "", f"the answer is not JSON ({error.msg} at position {error.pos})"
        ) from error
    check(value, schema)
    return value
