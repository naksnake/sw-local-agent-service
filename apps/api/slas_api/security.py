"""Passwords, session tokens and one-time passwords (ADR-0007).

argon2id at time cost 3, 64 MiB, parallelism 4 is the only production profile. The `test`
profile exists so the unit suite does not spend a minute hashing; it is chosen only through
an explicit `SLAS_ARGON2_PROFILE=test` and a test asserts the default is production.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from slas_api.settings import Argon2Profile
from slas_schemas.errors import ThreePartMessage

MIN_PASSWORD_LENGTH: Final = 12
SESSION_TOKEN_BYTES: Final = 32  # 256 bits
#: Readable one-time passwords: no 0/O, 1/l/I; four groups of four is 19 characters.
_OTP_ALPHABET: Final = "abcdefghjkmnpqrstuvwxyz23456789"
_OTP_GROUPS: Final = 4
_OTP_GROUP_LENGTH: Final = 4

_PRODUCTION: Final = {"time_cost": 3, "memory_cost": 64 * 1024, "parallelism": 4}
_TEST: Final = {"time_cost": 1, "memory_cost": 8 * 1024, "parallelism": 1}


class Passwords:
    """Hash and verify with argon2id; `needs_rehash` says when parameters moved on."""

    def __init__(self, profile: Argon2Profile = "production") -> None:
        params = _PRODUCTION if profile == "production" else _TEST
        self.profile = profile
        self._hasher = PasswordHasher(
            time_cost=int(params["time_cost"]),
            memory_cost=int(params["memory_cost"]),
            parallelism=int(params["parallelism"]),
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        # Verified against when no person matches, so a wrong email costs the same time.
        self._decoy = self._hasher.hash(secrets.token_urlsafe(24))

    @property
    def parameters(self) -> dict[str, int]:
        return {
            "time_cost": self._hasher.time_cost,
            "memory_cost": self._hasher.memory_cost,
            "parallelism": self._hasher.parallelism,
        }

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        target = password_hash if password_hash else self._decoy
        try:
            return bool(self._hasher.verify(target, password)) and password_hash is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return bool(self._hasher.check_needs_rehash(password_hash))
        except InvalidHashError:
            return True


def check_password_policy(password: str, email: str) -> ThreePartMessage | None:
    """None when the password is acceptable, else the sentences from docs/ui/sign-in.md."""
    if len(password) < MIN_PASSWORD_LENGTH:
        count = len(password)
        return ThreePartMessage(
            f"The password is too short: it has {count} "
            f"{'character' if count == 1 else 'characters'} and needs at least "
            f"{MIN_PASSWORD_LENGTH}.",
            "Short passwords are quick to guess.",
            "Add a few more words.",
        )
    if password.strip().lower() == email.strip().lower():
        return ThreePartMessage(
            "The password can't be your email address.",
            "Everyone you write to knows it.",
            "Choose something only you know.",
        )
    return None


def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_one_time_password() -> str:
    groups = (
        "".join(secrets.choice(_OTP_ALPHABET) for _ in range(_OTP_GROUP_LENGTH))
        for _ in range(_OTP_GROUPS)
    )
    return "-".join(groups)
