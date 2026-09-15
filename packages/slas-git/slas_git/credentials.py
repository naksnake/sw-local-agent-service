"""Credentials stored encrypted, by reference (CLAUDE.md §5.7 storage, INV-14).

    Remote.credential_ref ──► CredentialStore ──► Sealer (AES-GCM) ──► ciphertext at rest

A `Sealer` turns plaintext into ciphertext with a key derived from `SLAS_SECRET_KEY`
(quickstart) or fetched from Vault KV (prod); the `CredentialStore` keeps ciphertext under an
opaque reference and decrypts only for one operation, in memory. The UI ever sees a
fingerprint: the last four characters of a token, or the SSH public-key fingerprint.

The AES-GCM primitive is not in the standard library. `AesGcmSealer` binds to the
`cryptography` package when it is approved and installed; until then it says so in three
parts, and tests run against `FakeSealer`, which is a reversible transform for tests only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_schemas.vault import VaultError, VaultKv

AuthType = Literal["pat", "ssh_key"]
KEY_PURPOSE: Final = b"slas-git-broker/credentials/v1"


class CredentialError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def derive_key(secret_key: str, *, purpose: bytes = KEY_PURPOSE, length: int = 32) -> bytes:
    """HKDF-SHA256 (RFC 5869) with a fixed salt: the same SLAS_SECRET_KEY always yields the
    same sealing key, and a different purpose yields an unrelated one."""
    if len(secret_key) < 32:
        raise CredentialError(
            ThreePartMessage(
                "SLAS_SECRET_KEY is too short to derive an encryption key.",
                "It must be at least 32 characters; install.sh generates 43.",
                "Regenerate it with `slas doctor --fix-env` or copy the value install.sh wrote.",
            )
        )
    salt = hashlib.sha256(b"slas-git-broker").digest()
    prk = hmac.new(salt, secret_key.encode("utf-8"), hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + purpose + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


class Sealer(Protocol):
    name: str

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes: ...

    def open(self, ciphertext: bytes, *, aad: bytes) -> bytes: ...


class FakeSealer:
    """Tests only: a keyed, reversible transform with an integrity tag. Not encryption."""

    name = "fake-for-tests"

    def __init__(self, key: bytes) -> None:
        self.key = key

    def _stream(self, nonce: bytes, length: int) -> bytes:
        out = b""
        counter = 0
        while len(out) < length:
            out += hashlib.sha256(self.key + nonce + counter.to_bytes(4, "big")).digest()
            counter += 1
        return out[:length]

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        body = bytes(
            a ^ b for a, b in zip(plaintext, self._stream(nonce, len(plaintext)), strict=True)
        )
        tag = hmac.new(self.key, nonce + aad + body, hashlib.sha256).digest()[:16]
        return nonce + tag + body

    def open(self, ciphertext: bytes, *, aad: bytes) -> bytes:
        nonce, tag, body = ciphertext[:12], ciphertext[12:28], ciphertext[28:]
        expected = hmac.new(self.key, nonce + aad + body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected):
            raise CredentialError(
                ThreePartMessage(
                    "The stored credential could not be opened.",
                    "Its integrity check failed: the file was altered or SLAS_SECRET_KEY changed.",
                    "Rotate the credential from Settings → Git remotes.",
                )
            )
        return bytes(a ^ b for a, b in zip(body, self._stream(nonce, len(body)), strict=True))


class AesGcmSealer:
    """AES-256-GCM via the `cryptography` package. Refuses to construct until it is there."""

    name = "aes-gcm"

    def __init__(self, key: bytes) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import (  # type: ignore[import-not-found,unused-ignore]
                AESGCM,
            )
        except ImportError as exc:
            raise CredentialError(
                ThreePartMessage(
                    "The AES-GCM sealer is not available on this host.",
                    "It needs the `cryptography` package, which has not been approved and "
                    "installed yet (CLAUDE.md §0.3: ask before adding a dependency).",
                    "Approve and pin `cryptography`, then restart git-broker.",
                )
            ) from exc
        if len(key) != 32:
            raise ValueError("AES-256-GCM needs a 32-byte key")
        self._aead = AESGCM(key)

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + bytes(self._aead.encrypt(nonce, plaintext, aad))

    def open(self, ciphertext: bytes, *, aad: bytes) -> bytes:
        return bytes(self._aead.decrypt(ciphertext[:12], ciphertext[12:], aad))


class StoredCredential(SlasModel):
    ref: str = Field(pattern=r"^cred-[0-9a-f]{24}$")
    owner: str = Field(min_length=1)
    auth_type: AuthType
    sealer: str
    ciphertext_b64: str
    fingerprint: str
    created_at: datetime
    rotated_at: datetime | None = None


class CredentialStore(Protocol):
    def put(
        self, secret: str, *, owner: str, auth_type: AuthType, fingerprint: str, now: datetime
    ) -> str: ...

    def reveal(self, ref: str, *, owner: str) -> str: ...

    def rotate(
        self, ref: str, secret: str, *, owner: str, fingerprint: str, now: datetime
    ) -> None: ...

    def delete(self, ref: str, *, owner: str) -> None: ...

    def fingerprint_of(self, ref: str) -> str: ...


class EncryptedFileStore:
    """Ciphertext by reference in one JSON file, mode 0600, written atomically.

    Stands in for the `postgres+aesgcm` store of the compose profile behind the same
    interface; Vault KV (prod) is another implementation of `CredentialStore`.
    """

    def __init__(self, path: Path, sealer: Sealer) -> None:
        self.path = path
        self.sealer = sealer

    def _load(self) -> dict[str, StoredCredential]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {ref: StoredCredential.model_validate(item) for ref, item in raw.items()}

    def _save(self, items: dict[str, StoredCredential]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {ref: item.model_dump(mode="json") for ref, item in items.items()}
        write_atomic(self.path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def _get(self, items: dict[str, StoredCredential], ref: str, owner: str) -> StoredCredential:
        item = items.get(ref)
        if item is None or item.owner != owner:
            raise CredentialError(
                ThreePartMessage(
                    "That credential is not available.",
                    "It was deleted, or it belongs to someone else.",
                    "Add the remote again under Settings → Git remotes.",
                )
            )
        return item

    def put(
        self, secret: str, *, owner: str, auth_type: AuthType, fingerprint: str, now: datetime
    ) -> str:
        items = self._load()
        ref = f"cred-{secrets.token_hex(12)}"
        sealed = self.sealer.seal(secret.encode("utf-8"), aad=f"{ref}|{owner}".encode())
        items[ref] = StoredCredential(
            ref=ref,
            owner=owner,
            auth_type=auth_type,
            sealer=self.sealer.name,
            ciphertext_b64=base64.b64encode(sealed).decode("ascii"),
            fingerprint=fingerprint,
            created_at=now,
        )
        self._save(items)
        return ref

    def reveal(self, ref: str, *, owner: str) -> str:
        item = self._get(self._load(), ref, owner)
        sealed = base64.b64decode(item.ciphertext_b64)
        return self.sealer.open(sealed, aad=f"{ref}|{owner}".encode()).decode("utf-8")

    def rotate(self, ref: str, secret: str, *, owner: str, fingerprint: str, now: datetime) -> None:
        items = self._load()
        item = self._get(items, ref, owner)
        sealed = self.sealer.seal(secret.encode("utf-8"), aad=f"{ref}|{owner}".encode())
        items[ref] = item.model_copy(
            update={
                "ciphertext_b64": base64.b64encode(sealed).decode("ascii"),
                "fingerprint": fingerprint,
                "rotated_at": now,
            }
        )
        self._save(items)

    def delete(self, ref: str, *, owner: str) -> None:
        items = self._load()
        self._get(items, ref, owner)
        del items[ref]
        self._save(items)

    def fingerprint_of(self, ref: str) -> str:
        item = self._load().get(ref)
        return item.fingerprint if item else "unknown"


def token_fingerprint(token: str) -> str:
    """What the UI shows for a token after save: the last four characters only."""
    tail = token.strip()[-4:]
    return f"…{tail}" if len(token.strip()) >= 8 else "…"


def sha256_fingerprint(public_key_blob: bytes) -> str:
    digest = hashlib.sha256(public_key_blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def looks_like_private_key(text: str) -> bool:
    return "-----BEGIN" in text and "PRIVATE KEY-----" in text


def wipe(buffer: bytearray) -> None:
    """Overwrite a secret held in a mutable buffer before it is dropped."""
    for index in range(len(buffer)):
        buffer[index] = 0


def secret_key_from_env(environ: dict[str, str] | os._Environ[str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    value = env.get("SLAS_SECRET_KEY", "")
    if not value:
        raise CredentialError(
            ThreePartMessage(
                "SLAS_SECRET_KEY is not set for git-broker.",
                "install.sh writes it into the data root's .env; the broker reads it at start.",
                "Run ./install.sh again, or set SLAS_SECRET_KEY in .env and restart git-broker.",
            )
        )
    return value


class VaultCredentialStore:
    """prod (`CRED_STORE=vault`): the secret itself lives in Vault KV under
    `<mount>/git/<ref>` with the owner and fingerprint as data; git-broker reads it for one
    operation with its AppRole token. Nothing is written to a workspace or a log (INV-14)."""

    PREFIX = "git"

    def __init__(self, kv: VaultKv) -> None:
        self.kv = kv

    def _path(self, ref: str) -> str:
        return f"{self.PREFIX}/{ref}"

    def _read(self, ref: str, owner: str) -> dict[str, str]:
        try:
            data = self.kv.read(self._path(ref))
        except VaultError as exc:
            raise CredentialError(
                ThreePartMessage(
                    "That credential is not available.",
                    exc.message.likely_cause,
                    "Add the remote again under Settings → Git remotes.",
                )
            ) from None
        if data.get("owner") != owner:
            raise CredentialError(
                ThreePartMessage(
                    "That credential is not available.",
                    "It belongs to someone else.",
                    "Add the remote again under Settings → Git remotes.",
                )
            )
        return data

    def put(
        self, secret: str, *, owner: str, auth_type: AuthType, fingerprint: str, now: datetime
    ) -> str:
        ref = f"cred-{secrets.token_hex(12)}"
        try:
            self.kv.write(
                self._path(ref),
                {
                    "secret": secret,
                    "owner": owner,
                    "auth_type": auth_type,
                    "fingerprint": fingerprint,
                    "created_at": now.isoformat(),
                },
            )
        except VaultError as exc:
            raise CredentialError(exc.message) from None
        return ref

    def reveal(self, ref: str, *, owner: str) -> str:
        return self._read(ref, owner)["secret"]

    def rotate(self, ref: str, secret: str, *, owner: str, fingerprint: str, now: datetime) -> None:
        current = self._read(ref, owner)
        try:
            self.kv.write(
                self._path(ref),
                {
                    **current,
                    "secret": secret,
                    "fingerprint": fingerprint,
                    "rotated_at": now.isoformat(),
                },
            )
        except VaultError as exc:
            raise CredentialError(exc.message) from None

    def delete(self, ref: str, *, owner: str) -> None:
        self._read(ref, owner)
        try:
            self.kv.delete(self._path(ref))
        except VaultError as exc:
            raise CredentialError(exc.message) from None

    def fingerprint_of(self, ref: str) -> str:
        try:
            return self.kv.read(self._path(ref)).get("fingerprint", "unknown")
        except VaultError:
            return "unknown"
