"""A private key on tmpfs for exactly one operation, shredded after (CLAUDE.md §5.7 use).

    GIT_SSH_COMMAND="ssh -i <f> -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes
                     -o UserKnownHostsFile=<pinned> -o BatchMode=yes"

`key_dir` is the broker's `/run/slas-keys` tmpfs (mode 700). The key file is created 0600
with O_EXCL, overwritten with zeros and unlinked in `finally`, whatever happened.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage


class SshKeyError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class SshEnv(SlasModel):
    key_path: str
    known_hosts_path: str
    env: dict[str, str] = Field(default_factory=dict)


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
    """Overwrite with zeros, flush, unlink. Best effort on a missing file."""
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
def key_file(
    key_dir: Path, private_key: str, *, known_hosts_line: str, tag: str
) -> Iterator[SshEnv]:
    if not known_hosts_line.strip():
        raise SshKeyError(
            ThreePartMessage(
                "No pinned host key is available for this SSH remote.",
                "SSH runs with StrictHostKeyChecking=yes and only trusts a key an administrator "
                "pinned under Admin → Git hosts.",
                "Ask an administrator to pin the host key, or use an https remote with a token.",
            )
        )
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    nonce = secrets.token_hex(6)
    key_path = key_dir / f"key-{tag}-{nonce}"
    known_path = key_dir / f"known_hosts-{tag}-{nonce}"
    try:
        _write_private(key_path, private_key)
        _write_private(known_path, known_hosts_line.strip())
        command = (
            f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={known_path} -o BatchMode=yes -o PasswordAuthentication=no"
        )
        yield SshEnv(
            key_path=str(key_path),
            known_hosts_path=str(known_path),
            env={"GIT_SSH_COMMAND": command},
        )
    finally:
        shred(key_path)
        known_path.unlink(missing_ok=True)
