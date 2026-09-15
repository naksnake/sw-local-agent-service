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
from pathlib import Path
from typing import Final, Protocol

from slas_schemas.errors import ThreePartMessage
from slas_schemas.vault import VaultError, VaultKv

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


class LocalCredentialResolver:
    """`env:NAME` from the environment and `file:PATH` from a file the platform wrote itself
    (0600, for example a per-station batch key under Factory/keys). Relative paths resolve
    under `root`."""

    def __init__(self, *, root: Path, environ: Mapping[str, str] | None = None) -> None:
        self.root = root
        self.env = EnvCredentialResolver(environ)

    def resolve(self, ref: str) -> str:
        check_ref(ref)
        kind, _, name = ref.partition(":")
        if kind == "env":
            return self.env.resolve(ref)
        if kind == "file":
            path = Path(name)
            if not path.is_absolute():
                path = self.root / path
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise CredentialError(
                    ThreePartMessage(
                        f"The credential {ref} could not be read.",
                        str(exc),
                        "Check that the file exists and the executor may read it.",
                    )
                ) from None
            if not value:
                raise CredentialError(
                    ThreePartMessage(
                        f"The credential {ref} is empty.",
                        "The file exists but holds nothing.",
                        "Rotate the credential so the file is written again.",
                    )
                )
            return value
        raise CredentialError(
            ThreePartMessage(
                f"This installation cannot resolve {kind}: references.",
                "Vault arrives with the prod profile.",
                "Use env:NAME or file:PATH.",
            )
        )


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


class VaultCredentialResolver:
    """prod: `vault:<mount>/<path>/<key>` is read from Vault KV v2 at dispatch (CLAUDE.md §3,
    INV-5). The last segment names the key inside the secret, the first the mount, the rest
    the secret's path: `vault:slas/lab/gx8-01/bmc/password` → mount `slas`, path
    `lab/gx8-01/bmc`, key `password`. The secret is returned to the driver for one operation
    and never written anywhere."""

    def __init__(self, kv: VaultKv, *, fallback: CredentialResolver | None = None) -> None:
        self.kv = kv
        #: `env:` and `file:` references still resolve (the batch keys the platform wrote).
        self.fallback = fallback
        self.asked: list[str] = []

    def resolve(self, ref: str) -> str:
        check_ref(ref)
        kind, _, rest = ref.partition(":")
        if kind != "vault":
            if self.fallback is None:
                raise CredentialError(
                    ThreePartMessage(
                        f"This installation resolves vault: references only, not {kind}:.",
                        "The prod profile keeps every credential in Vault.",
                        "Move the secret into Vault and reference it as "
                        "vault:<mount>/<path>/<key>.",
                    )
                )
            return self.fallback.resolve(ref)
        parts = [part for part in rest.split("/") if part]
        if len(parts) < 3:
            raise CredentialError(
                ThreePartMessage(
                    f"The reference {ref} is too short.",
                    "A Vault reference is vault:<mount>/<path…>/<key>, at least three parts.",
                    "Name the mount, the secret's path and the key inside it.",
                )
            )
        mount, key = parts[0], parts[-1]
        path = "/".join(parts[1:-1])
        if mount != self.kv.mount:
            raise CredentialError(
                ThreePartMessage(
                    f"The reference {ref} names the mount {mount}, but this service reads "
                    f"{self.kv.mount}.",
                    "Each service is logged in to one KV mount (VAULT_KV_MOUNT).",
                    f"Reference the secret under vault:{self.kv.mount}/…, or move it there.",
                )
            )
        self.asked.append(ref)
        try:
            data = self.kv.read(path)
        except VaultError as exc:
            raise CredentialError(exc.message) from None
        value = data.get(key, "")
        if not value:
            raise CredentialError(
                ThreePartMessage(
                    f"The secret at {mount}/{path} has no {key}.",
                    "The key is missing or empty in Vault.",
                    f"Write it: vault kv put {mount}/{path} {key}=…",
                )
            )
        return value


def resolver_for(
    profile: str,
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
    kv: VaultKv | None = None,
) -> CredentialResolver:
    """What an executor builds at start: env and file references in quickstart; Vault with
    the same fallback in prod (`CREDENTIAL_SOURCE=vault`)."""
    env = dict(os.environ if environ is None else environ)
    local: CredentialResolver = (
        LocalCredentialResolver(root=root, environ=env) if root else EnvCredentialResolver(env)
    )
    source = env.get("CREDENTIAL_SOURCE", "vault" if profile == "prod" else "env")
    if source != "vault":
        return local
    if kv is None:
        raise CredentialError(
            ThreePartMessage(
                "CREDENTIAL_SOURCE is vault, but this service has no Vault client.",
                "The prod profile logs each executor in to Vault with AppRole at start.",
                "Check VAULT_ADDR, VAULT_CACERT, VAULT_ROLE_ID_FILE and VAULT_SECRET_ID_FILE "
                "in compose/prod.override.yml and the service's start-up log.",
            )
        )
    return VaultCredentialResolver(kv, fallback=local)
