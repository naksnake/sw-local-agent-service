"""Process settings for git-broker: environment for facts, a file for the one secret.

    SLAS_DATA_ROOT        /data                     Coding/<user>/Projects, .git-broker/
    SLAS_SECRETS_DIR      /run/secrets              secret_key lives here (compose `secrets:`)
    GIT_HOSTS_ALLOWLIST   /etc/slas/git-hosts.yaml  the allowlist, mounted rw for POST /v1/hosts
    SLAS_KEY_DIR          /run/slas-keys            tmpfs for per-operation SSH key files
    SLAS_ASKPASS_DIR      <data root>/.git-broker/bin   where the GIT_ASKPASS helper is written
    SLAS_SEALER           aes-gcm                   or fake-for-tests (obfuscation, not for prod)
    SLAS_CA_BUNDLE        (empty)                   CA file for the hosts' REST APIs
    SLAS_GIT_PATH         /usr/bin:/bin             PATH for git and ssh-keygen
    SLAS_BIND             0.0.0.0:8000

The only secret, `SLAS_SECRET_KEY`, is read from the secrets file and never logged; the
environment variable of the same name is a fallback for a host without Docker secrets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from slas_git.credentials import secret_key_from_env
from slas_http.serve import DEFAULT_BIND

SECRET_KEY_SECRET: Final = "secret_key"  # noqa: S105 — a file name, not a secret
BROKER_DIR: Final = ".git-broker"
SEALERS: Final = ("aes-gcm", "fake-for-tests")


@dataclass(frozen=True)
class Settings:
    data_root: Path = Path("/data")
    secrets_dir: Path = Path("/run/secrets")
    hosts_file: Path = Path("/etc/slas/git-hosts.yaml")
    key_dir: Path = Path("/run/slas-keys")
    askpass_dir: Path | None = None
    sealer: str = "aes-gcm"
    ca_bundle: str | None = None
    git_path: str = "/usr/bin:/bin"
    bind: str = DEFAULT_BIND

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = environ if environ is not None else os.environ
        data_root = Path(env.get("SLAS_DATA_ROOT") or "/data")
        askpass = env.get("SLAS_ASKPASS_DIR") or ""
        sealer = env.get("SLAS_SEALER") or "aes-gcm"
        if sealer not in SEALERS:
            raise ValueError(f"SLAS_SEALER must be one of {', '.join(SEALERS)}, not {sealer!r}.")
        return cls(
            data_root=data_root,
            secrets_dir=Path(env.get("SLAS_SECRETS_DIR") or "/run/secrets"),
            hosts_file=Path(env.get("GIT_HOSTS_ALLOWLIST") or "/etc/slas/git-hosts.yaml"),
            key_dir=Path(env.get("SLAS_KEY_DIR") or "/run/slas-keys"),
            askpass_dir=Path(askpass) if askpass else None,
            sealer=sealer,
            ca_bundle=env.get("SLAS_CA_BUNDLE") or None,
            git_path=env.get("SLAS_GIT_PATH") or "/usr/bin:/bin",
            bind=env.get("SLAS_BIND") or DEFAULT_BIND,
        )

    # --- derived paths --------------------------------------------------------------------

    @property
    def broker_dir(self) -> Path:
        return self.data_root / BROKER_DIR

    @property
    def credentials_path(self) -> Path:
        return self.broker_dir / "credentials.json"

    @property
    def remotes_path(self) -> Path:
        return self.broker_dir / "remotes.json"

    @property
    def audit_path(self) -> Path:
        return self.broker_dir / "audit.jsonl"

    @property
    def askpass_directory(self) -> Path:
        return self.askpass_dir if self.askpass_dir is not None else self.broker_dir / "bin"

    def read_secret_key(self, environ: Mapping[str, str] | None = None) -> str:
        """`secret_key` from the secrets directory, else SLAS_SECRET_KEY; three parts if neither."""
        path = self.secrets_dir / SECRET_KEY_SECRET
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
        env = dict(environ) if environ is not None else dict(os.environ)
        return secret_key_from_env(env)
