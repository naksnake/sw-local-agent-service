"""GIT_ASKPASS with the token on an inherited pipe (CLAUDE.md §5.7 use).

    broker: token ──► os.pipe() write end (closed at once) ──► read end inherited by git
    git: needs a password ──► runs $GIT_ASKPASS ──► helper reads $SLAS_ASKPASS_FD ──► token

The token is never in argv, never in the environment, never in the URL and never on disk.
The account name (`oauth2`, `x-access-token`, …) is not a secret and travels in the URL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from slas_schemas.envfile import write_atomic

ASKPASS_SCRIPT: Final = """#!/usr/bin/env python3
# GIT_ASKPASS helper for SW Local Agent Service git-broker (CLAUDE.md §5.7).
# Answers git's password prompt with the token read from the inherited fd named by
# SLAS_ASKPASS_FD. The token is never an argument and never in the environment.
import os
import sys

prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
if "username" in prompt:
    sys.stdout.write(os.environ.get("SLAS_ASKPASS_USERNAME", "oauth2") + "\\n")
    sys.exit(0)
fd = int(os.environ["SLAS_ASKPASS_FD"])
chunks = []
while True:
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    chunks.append(chunk)
sys.stdout.write(b"".join(chunks).decode("utf-8").strip() + "\\n")
"""

ASKPASS_NAME: Final = "slas-askpass"


def install_askpass(directory: Path) -> Path:
    """Write the helper as an executable file the broker owns; idempotent."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ASKPASS_NAME
    write_atomic(path, ASKPASS_SCRIPT, mode=0o755)
    return path


@contextmanager
def token_pipe(token: str) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield (read fd to pass to git, environment additions); the fd is closed afterwards."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, token.strip().encode("utf-8") + b"\n")
    finally:
        os.close(write_fd)
    try:
        yield read_fd, {"SLAS_ASKPASS_FD": str(read_fd), "GIT_TERMINAL_PROMPT": "0"}
    finally:
        os.close(read_fd)
