"""The CI grep over log sinks (CLAUDE.md §5.7 audit, INV-5): every file under a directory is
searched for credential shapes and for the actual secrets a test used. Standard library only
so CI can run it as `python -m slas_hal.sinkcheck <dir> [--known SECRET …]`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SECRET_SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("basic_auth_url", re.compile(r"(?i)\b(?:https?|ssh)://[^/\s:@]+:[^@\s/]+@")),
    (
        "authorization_header",
        re.compile(r"(?i)\bAuthorization(?:\s*[:=]\s*)(?:Basic|Bearer)\s+\S+"),
    ),
    ("ipmi_password_env", re.compile(r"\bIPMI_PASSWORD\s*[=:]\s*\S+")),
    ("ipmitool_password_argv", re.compile(r"\bipmitool\b[^\n]*\s-P\s+\S+")),
    ("sshpass", re.compile(r"\bsshpass\s+-p\s+\S+")),
    (
        "password_assignment",
        re.compile(r"(?i)\b(?:password|passwd|secret_key|api_key)\s*[=:]\s*['\"]?[^\s'\",;]{8,}"),
    ),
)
SKIP_DIRS: Final = frozenset({".git", "node_modules", ".venv", "__pycache__"})


@dataclass(frozen=True)
class SinkHit:
    path: str
    names: tuple[str, ...]

    def sentence(self) -> str:
        return f"{self.path} contains {', '.join(self.names)}."


def find_secrets(text: str, known: Iterable[str] = ()) -> list[str]:
    found: list[str] = []
    for secret in known:
        if secret and secret in text:
            found.append(f"known secret ({secret[:3]}…)")
    for name, pattern in SECRET_SHAPES:
        if any("[redacted" not in m.group(0) for m in pattern.finditer(text)):
            found.append(name)
    return found


def scan_tree(root: Path, known: Iterable[str] = ()) -> list[SinkHit]:
    known = [k for k in known if k]
    hits: list[SinkHit] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable: not a log sink
            names = find_secrets(text, known)
            if names:
                hits.append(SinkHit(path=str(path.relative_to(root)), names=tuple(names)))
    return hits


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m slas_hal.sinkcheck",
        description="Grep every file under a directory for credential shapes and known secrets.",
    )
    parser.add_argument("root", help="Directory of log sinks, run artefacts and bundles to check.")
    parser.add_argument(
        "--known", action="append", default=[], help="A secret that must not appear (repeatable)."
    )
    parser.add_argument(
        "--known-env",
        action="append",
        default=[],
        help="Name of an environment variable whose value must not appear (repeatable).",
    )
    args = parser.parse_args(argv)
    known = [*args.known, *(os.environ.get(name, "") for name in args.known_env)]
    root = Path(args.root)
    if not root.is_dir():
        print(f"{root} is not a directory; nothing to check.")
        return 2
    hits = scan_tree(root, known)
    if hits:
        noun = "file" if len(hits) == 1 else "files"
        print(f"{len(hits)} {noun} under {root} contain a credential:")
        for hit in hits:
            print(f"  {hit.sentence()}")
        return 1
    print(f"No credential shape or known secret in any file under {root}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
