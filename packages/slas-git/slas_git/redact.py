"""Credential redaction at the logging boundary (CLAUDE.md §5.7 audit, INV-5, INV-14).

Every string that reaches an audit row, a log line, a terminal transcript or a model goes
through `redact()`. `find_secrets()` is the CI grep: given the actual secrets a test used,
it reports every sink that still contains one of them, or any text matching a token or
private-key shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

SECRET_SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("basic_auth_url", re.compile(r"(?i)\b(https?|ssh)://([^/\s:@]+):([^@\s/]+)@")),
    ("authorization_header", re.compile(r"(?i)\b(Authorization|PRIVATE-TOKEN)(\s*[:=]\s*)(\S+)")),
    ("askpass_answer", re.compile(r"(?i)\b(password|token|secret)(\s*[=:]\s*)([^\s,;]{8,})")),
)


def redact(text: str) -> str:
    """Replace every credential-shaped run; the non-secret part of a pair is kept."""
    out = text
    for name, pattern in SECRET_SHAPES:
        if name == "basic_auth_url":
            out = pattern.sub(r"\1://\2:[redacted]@", out)
        elif name in ("authorization_header", "askpass_answer"):
            out = pattern.sub(rf"\1\2[redacted:{name}]", out)
        else:
            out = pattern.sub(f"[redacted:{name}]", out)
    return out


def find_secrets(text: str, known: Iterable[str] = ()) -> list[str]:
    """Names of every leak in `text`: a known secret verbatim, or a credential shape."""
    found: list[str] = []
    for secret in known:
        if secret and secret in text:
            found.append(f"known secret ({secret[:4]}…)")
    for name, pattern in SECRET_SHAPES:
        if name in ("askpass_answer",):
            continue  # too broad for a grep over prose; the known-secret check covers it
        # A redaction marker left in place is the shape's non-secret residue, not a leak.
        if any("[redacted" not in match.group(0) for match in pattern.finditer(text)):
            found.append(name)
    return found
