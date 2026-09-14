"""Credentials for targets are resolved at dispatch, never stored on a record (INV-5).

A `TargetRecord` carries opaque references such as `env:LAB_GX8_01_BMC_PASSWORD` or
`vault:kv/lab/gx8-01/bmc`. A `CredentialResolver` turns a reference into the secret inside
the driver, for one operation, and the secret goes into an environment variable or a 0600
file for the child process — never argv, never a URL, never a log line.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Final, Protocol

from slas_schemas.errors import ThreePartMessage

CREDENTIAL_REF: Final = re.compile(r"^(env|vault|file):[A-Za-z0-9_./-]{1,200}$")


class CredentialError(ValueError):
    """A ValueError so pydantic turns a bad reference on a record into a validation error."""

    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def check_ref(ref: str) -> str:
    if not CREDENTIAL_REF.match(ref):
        # The value is not echoed: if it is the secret itself, it must not land in a log.
        raise CredentialError(
            ThreePartMessage(
                "This credential field is not a reference.",
                "A reference names where the secret lives: env:NAME, vault:PATH or file:PATH; "
                "the secret itself never goes on a target record.",
                "Put the secret in the vault or the .env and reference it by name.",
            )
        )
    return ref


class CredentialResolver(Protocol):
    def resolve(self, ref: str) -> str: ...


class EnvCredentialResolver:
    """Quickstart: `env:NAME` reads the executor's environment (Docker secrets → env)."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = dict(os.environ if environ is None else environ)

    def resolve(self, ref: str) -> str:
        check_ref(ref)
        kind, _, name = ref.partition(":")
        if kind != "env":
            raise CredentialError(
                ThreePartMessage(
                    f"This installation cannot resolve {kind}: references.",
                    "Quickstart keeps target credentials in the executor's environment; Vault "
                    "arrives with the prod profile.",
                    f"Use env:{name.upper().replace('/', '_').replace('-', '_')} and set it "
                    "in .env, or switch to the prod profile.",
                )
            )
        value = self.environ.get(name)
        if not value:
            raise CredentialError(
                ThreePartMessage(
                    f"The credential {ref} is not set.",
                    "The executor's environment has no such variable, or it is empty.",
                    f"Add {name} to .env (Docker secret in prod) and restart the executor.",
                )
            )
        return value


class FakeCredentialResolver:
    """Tests: a dictionary, plus a record of every reference asked for."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self.secrets = dict(secrets)
        self.asked: list[str] = []

    def resolve(self, ref: str) -> str:
        check_ref(ref)
        self.asked.append(ref)
        try:
            return self.secrets[ref]
        except KeyError:
            raise CredentialError(
                ThreePartMessage(
                    f"The credential {ref} is not set.",
                    "The fake resolver has no entry for it.",
                    "Add it to the resolver in the test.",
                )
            ) from None
