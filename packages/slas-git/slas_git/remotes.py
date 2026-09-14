"""Remotes: a repository a person registered with a credential reference (CLAUDE.md §5.7).

    Remote {id, owner, name, uri (no secret), host, auth_type, credential_ref, fingerprint,
            allowed_ops, default_branch, last_used}

The secret itself never lives here: it is sealed in the `CredentialStore` and the remote
carries only the reference and a fingerprint the UI may show.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from slas_git.credentials import (
    AuthType,
    CredentialStore,
    looks_like_private_key,
    token_fingerprint,
)
from slas_git.hosts import GitHost, GitHosts, RemoteUri, host_for, parse_remote_uri
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage

GitOp = Literal["clone", "pull", "push_branch", "push_protected", "ls_remote", "bundle"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")


def _default_ops() -> list[GitOp]:
    return ["ls_remote", "clone", "pull", "push_branch"]


class Remote(SlasModel):
    id: str = Field(pattern=r"^rem-[0-9a-f]{16}$")
    owner: str = Field(min_length=1)
    name: str = Field(min_length=2, max_length=48)
    uri: str = Field(min_length=1)
    host: str = Field(min_length=1)
    auth_type: AuthType
    credential_ref: str = Field(pattern=r"^cred-[0-9a-f]{24}$")
    fingerprint: str = Field(min_length=1)
    allowed_ops: list[GitOp] = Field(default_factory=_default_ops)
    default_branch: str = "main"
    created_at: datetime
    last_used: datetime | None = None
    expires_at: datetime | None = None

    def parsed(self) -> RemoteUri:
        return parse_remote_uri(self.uri)

    def for_ui(self) -> dict[str, object]:
        """What the UI receives: never the credential, never the reference."""
        return {
            "id": self.id,
            "name": self.name,
            "uri": self.uri,
            "host": self.host,
            "auth_type": self.auth_type,
            "fingerprint": self.fingerprint,
            "default_branch": self.default_branch,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    def sentence(self) -> str:
        how = "token" if self.auth_type == "pat" else "SSH key"
        return (
            f"{self.name}: {self.uri}, {how} {self.fingerprint}, default branch "
            f"{self.default_branch}."
        )


class RemoteError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class RemoteStore:
    """Every remote in one JSON file, mode 0600, written atomically; scoped by owner."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Remote]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {rid: Remote.model_validate(item) for rid, item in raw.items()}

    def _save(self, items: dict[str, Remote]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(
            self.path,
            json.dumps({rid: r.model_dump(mode="json") for rid, r in items.items()}, indent=2)
            + "\n",
            mode=0o600,
        )

    def list_for(self, owner: str) -> list[Remote]:
        return sorted((r for r in self._load().values() if r.owner == owner), key=lambda r: r.name)

    def get(self, remote_id: str, *, owner: str) -> Remote:
        remote = self._load().get(remote_id)
        if remote is None or remote.owner != owner:
            raise RemoteError(
                ThreePartMessage(
                    "That remote is not one of yours.",
                    "It was deleted, or another person added it.",
                    "Pick a remote from Settings → Git remotes, or add one.",
                )
            )
        return remote

    def save(self, remote: Remote) -> None:
        items = self._load()
        items[remote.id] = remote
        self._save(items)

    def delete(self, remote_id: str, *, owner: str) -> Remote:
        items = self._load()
        remote = self.get(remote_id, owner=owner)
        del items[remote_id]
        self._save(items)
        return remote


def add_remote(
    *,
    owner: str,
    name: str,
    uri: str,
    auth_type: AuthType,
    secret: str,
    hosts: GitHosts,
    credentials: CredentialStore,
    remotes: RemoteStore,
    now: datetime,
    default_branch: str = "main",
    ssh_fingerprint: str | None = None,
) -> tuple[Remote, GitHost]:
    """Validate the address against the allowlist, seal the secret, store the remote."""
    if not _NAME.match(name):
        raise RemoteError(
            ThreePartMessage(
                f"{name!r} is not a usable remote name.",
                "Names are 2 to 48 lowercase letters, digits and dashes, such as gitlab-firmware.",
                "Choose another name.",
            )
        )
    if any(r.name == name for r in remotes.list_for(owner)):
        raise RemoteError(
            ThreePartMessage(
                f"You already have a remote called {name}.",
                "Remote names are unique per person so `Push to {name}` is unambiguous.",
                "Choose another name, or rotate the existing remote's credential.",
            )
        )
    parsed = parse_remote_uri(uri)
    host = host_for(parsed, hosts)
    secret = secret.strip()
    if auth_type == "pat":
        if parsed.scheme == "ssh":
            raise RemoteError(
                ThreePartMessage(
                    "A token goes with an https address.",
                    "Tokens are sent as the password of an https connection; ssh uses a key.",
                    "Use the https address of the repository, or choose SSH key.",
                )
            )
        if looks_like_private_key(secret) or len(secret) < 8:
            raise RemoteError(
                ThreePartMessage(
                    "The pasted value does not look like a token.",
                    "A token is one line of at least 8 characters; a private key is not a token.",
                    "Paste the token again, or choose SSH key.",
                )
            )
        fingerprint = token_fingerprint(secret)
    else:
        if parsed.scheme != "ssh":
            raise RemoteError(
                ThreePartMessage(
                    "An SSH key goes with an ssh address.",
                    "Keys are used with ssh://git@host/… or git@host:… addresses.",
                    "Use the ssh address of the repository, or choose Token.",
                )
            )
        if not looks_like_private_key(secret):
            raise RemoteError(
                ThreePartMessage(
                    "The pasted value is not a private key.",
                    "A key starts with -----BEGIN OPENSSH PRIVATE KEY----- (or another PRIVATE "
                    "KEY header).",
                    "Paste the private key of a deploy key made for this repository.",
                )
            )
        fingerprint = ssh_fingerprint or "SHA256:(unverified)"
    credential_ref = credentials.put(
        secret, owner=owner, auth_type=auth_type, fingerprint=fingerprint, now=now
    )
    remote = Remote(
        id=f"rem-{secrets.token_hex(8)}",
        owner=owner,
        name=name,
        uri=parsed.ssh_url() if parsed.scheme == "ssh" else parsed.https_url(),
        host=host.hostname,
        auth_type=auth_type,
        credential_ref=credential_ref,
        fingerprint=fingerprint,
        default_branch=default_branch,
        created_at=now,
    )
    remotes.save(remote)
    return remote, host
