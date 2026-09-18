"""Process settings for the model fetcher, read once from the environment (contract §3b).

    SLAS_MODELS_DIR   /data/Models             Models/<id>/, models.yaml, manifest.json
    SLAS_HUB_HOSTS    huggingface.co,cdn-lfs.huggingface.co,*.hf.co   the allowlist (ADR-0018)
    HF_ENDPOINT       https://huggingface.co   a mirror inside the perimeter, when set
    HTTPS_PROXY       (empty)                  honoured by urllib as it is
    HF_TOKEN_FILE     /run/secrets/hf_token    a token for gated repositories; empty = none
    SLAS_BIND         0.0.0.0:8000

The token is the only secret: read from the file at each fetch, sent as a header, never
logged, never in a URL, never returned (INV-5). The endpoint's host is always on the
allowlist, so a mirror needs HF_ENDPOINT alone.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from slas_fetch import DEFAULT_ENDPOINT, DEFAULT_HUB_HOSTS, Hub, allowlisted_opener, host_of
from slas_http.serve import DEFAULT_BIND

DEFAULT_MODELS_DIR: Final = "/data/Models"
DEFAULT_TOKEN_FILE: Final = "/run/secrets/hf_token"  # noqa: S105 — a file name, not a secret


def parse_hosts(text: str) -> tuple[str, ...]:
    hosts = tuple(part.strip().lower() for part in text.split(",") if part.strip())
    return hosts or DEFAULT_HUB_HOSTS


@dataclass(frozen=True)
class Settings:
    models_dir: Path = Path(DEFAULT_MODELS_DIR)
    hub_hosts: tuple[str, ...] = DEFAULT_HUB_HOSTS
    endpoint: str = DEFAULT_ENDPOINT
    proxy: str = ""
    token_file: Path = Path(DEFAULT_TOKEN_FILE)
    bind: str = DEFAULT_BIND

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def text(name: str, default: str) -> str:
            value = env.get(name, "").strip()
            return value or default

        return cls(
            models_dir=Path(text("SLAS_MODELS_DIR", DEFAULT_MODELS_DIR)),
            hub_hosts=parse_hosts(text("SLAS_HUB_HOSTS", ",".join(DEFAULT_HUB_HOSTS))),
            endpoint=text("HF_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
            proxy=text("HTTPS_PROXY", text("https_proxy", "")),
            token_file=Path(text("HF_TOKEN_FILE", DEFAULT_TOKEN_FILE)),
            bind=text("SLAS_BIND", DEFAULT_BIND),
        )

    @property
    def models_file(self) -> Path:
        return self.models_dir / "models.yaml"

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        """The allowlist plus the endpoint's own host, so a mirror needs HF_ENDPOINT alone."""
        endpoint_host = host_of(self.endpoint)
        if endpoint_host and endpoint_host not in self.hub_hosts:
            return (*self.hub_hosts, endpoint_host)
        return self.hub_hosts

    def read_token(self) -> str | None:
        try:
            value = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def hub(self) -> Hub:
        """A hub client for one fetch: the token read now, every request and redirect held to
        the allowlist."""
        return Hub(
            endpoint=self.endpoint,
            token=self.read_token(),
            opener=allowlisted_opener(self.allowed_hosts),
        )

    def sentence(self) -> str:
        proxy = f" through the proxy {self.proxy}" if self.proxy else ""
        return (
            f"Fetching from {self.endpoint}{proxy} into {self.models_dir}; allowed hosts: "
            f"{', '.join(self.allowed_hosts)}."
        )


def settings_from_environ() -> Settings:
    return Settings.from_env(os.environ)
