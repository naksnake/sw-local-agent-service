"""An SSH private key on tmpfs for exactly one command, shredded after (INV-5).

Same shape as the git-broker's key file: created 0600 with O_EXCL, zero-filled and unlinked
in `finally`. `known_hosts` is the administrator's pinned line, so ssh runs with
StrictHostKeyChecking=yes and refuses an unknown or changed host key.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field

from slas_schemas.common import SlasModel


class SshFiles(SlasModel):
    key_path: str
    known_hosts_path: str
    options: list[str] = Field(default_factory=list)


def _write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = content.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def shred(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, b"\0" * size)
        os.fsync(fd)
    finally:
        os.close(fd)
    path.unlink(missing_ok=True)


@contextmanager
def ssh_files(
    key_dir: Path, private_key: str, *, known_hosts_line: str, tag: str, connect_timeout_s: int
) -> Iterator[SshFiles]:
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    nonce = secrets.token_hex(6)
    key_path = key_dir / f"key-{tag}-{nonce}"
    known_path = key_dir / f"known_hosts-{tag}-{nonce}"
    try:
        _write_private(key_path, private_key)
        _write_private(known_path, known_hosts_line.strip())
        yield SshFiles(
            key_path=str(key_path),
            known_hosts_path=str(known_path),
            options=[
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_path}",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                f"ConnectTimeout={connect_timeout_s}",
                "-i",
                str(key_path),
            ],
        )
    finally:
        shred(key_path)
        known_path.unlink(missing_ok=True)
