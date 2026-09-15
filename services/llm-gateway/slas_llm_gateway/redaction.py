"""Redaction: credentials never enter a model context (INV-5).

Rules live in `config/redaction.yaml`; the shipped file is rendered from `DEFAULT_REDACTION`
and a test keeps the two in step. The gateway redacts every message before it reaches vLLM
and reports what it removed as counts per rule, never the values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from pydantic import Field, ValidationError, field_validator, model_validator

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage


class RedactionRule(SlasModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    #: Regex replacement; `None` means "[redacted:<name>]". Groups keep the non-secret part.
    replacement: str | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from exc
        return value

    def substitution(self) -> str:
        return self.replacement if self.replacement is not None else f"[redacted:{self.name}]"


class RedactionRules(SlasModel):
    version: int = 1
    rules: list[RedactionRule] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> RedactionRules:
        names = [rule.name for rule in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("rule names must be unique")
        return self


class RedactionError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def rules_from_mapping(data: object, *, source: str = "<memory>") -> RedactionRules:
    try:
        return RedactionRules.model_validate(data)
    except ValidationError as exc:
        raise RedactionError(
            ThreePartMessage(
                f"The redaction rules in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; the format is documented in config/README.md.",
            )
        ) from exc


@dataclass(frozen=True, slots=True)
class Redaction:
    text: str
    hits: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.hits)

    def sentence(self) -> str:
        if not self.hits:
            return "Nothing was redacted."
        total = sum(self.hits.values())
        noun = "secret" if total == 1 else "secrets"
        names = ", ".join(f"{count} {name}" for name, count in sorted(self.hits.items()))
        return f"{total} {noun} redacted before the model saw the text ({names})."


class Redactor:
    def __init__(self, rules: RedactionRules) -> None:
        self.rules = rules
        self._compiled = [(rule, re.compile(rule.pattern)) for rule in rules.rules]

    def redact(self, text: str) -> Redaction:
        hits: dict[str, int] = {}
        for rule, pattern in self._compiled:
            text, count = pattern.subn(rule.substitution(), text)
            if count:
                hits[rule.name] = hits.get(rule.name, 0) + count
        return Redaction(text, hits)


#: Order matters: multi-line blocks first, then structured tokens, then loose assignments.
DEFAULT_REDACTION: Final[dict[str, object]] = {
    "version": 1,
    "rules": [
        {
            "name": "private_key_block",
            "description": "PEM private keys (SSH deploy keys, TLS keys).",
            "pattern": (
                r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
            ),
        },
        {
            "name": "url_credentials",
            "description": "user:password inside a URL, as in https://user:token@host/.",
            "pattern": r"(://)[^/\s:@]+:[^/\s@]+@",
            "replacement": r"\1[redacted:url_credentials]@",
        },
        {
            "name": "bearer_token",
            "description": "Authorization: Bearer <token>.",
            "pattern": r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}",
            "replacement": r"\1 [redacted:bearer_token]",
        },
        {
            "name": "basic_auth",
            "description": "Authorization: Basic <base64>.",
            "pattern": r"(?i)\b(basic)\s+[A-Za-z0-9+/=]{16,}",
            "replacement": r"\1 [redacted:basic_auth]",
        },
        {
            "name": "github_token",
            "description": "GitHub personal, OAuth, server, user and refresh tokens.",
            "pattern": r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        },
        {
            "name": "gitlab_token",
            "description": "GitLab personal and project access tokens.",
            "pattern": r"\bglpat-[A-Za-z0-9_-]{20,}\b",
        },
        {
            "name": "aws_access_key",
            "description": "AWS access key ids (also used by MinIO-compatible stores).",
            "pattern": r"\bAKIA[0-9A-Z]{16}\b",
        },
        {
            "name": "ipmitool_password",
            "description": "The -P argument of ipmitool.",
            "pattern": r"(?i)(\bipmitool\b[^\n]*?\s-P)\s+\S+",
            "replacement": r"\1 [redacted:ipmitool_password]",
        },
        {
            "name": "password_assignment",
            "description": (
                "password=, passwd:, secret=, token=, api_key= and similar, including "
                "prefixed names such as POSTGRES_PASSWORD."
            ),
            "pattern": (
                r"(?i)\b([A-Za-z0-9_-]*?(?:password|passwd|pwd|secret|token|api[_-]?key))"
                r"(\s*[=:]\s*)['\"]?[^\s'\"]{4,}['\"]?"
            ),
            "replacement": r"\1\2[redacted:password_assignment]",
        },
    ],
}

REDACTION_FILE_HEADER: Final = (
    "Redaction rules for SW Local Agent Service (CLAUDE.md §5.2, INV-5).\n"
    "Rendered from slas_llm_gateway.redaction.DEFAULT_REDACTION; a unit test keeps file and\n"
    "code in step. The gateway applies these to every prompt before a model sees it, and the\n"
    "kernel applies them to logs before RCA. Rules run in order; `replacement` may keep the\n"
    "non-secret part with regex groups. Values are never logged, only counts per rule."
)


def default_redactor() -> Redactor:
    return Redactor(rules_from_mapping(DEFAULT_REDACTION, source="config/redaction.yaml"))


def render_redaction_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    rules = rules_from_mapping(data)
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {rules.version}")
    lines.append("rules:")
    for rule in rules.rules:
        lines.append(f"  - name: {rule.name}")
        lines.append(f"    description: {json.dumps(rule.description, ensure_ascii=False)}")
        lines.append(f"    pattern: {json.dumps(rule.pattern, ensure_ascii=False)}")
        if rule.replacement is not None:
            lines.append(f"    replacement: {json.dumps(rule.replacement, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"
