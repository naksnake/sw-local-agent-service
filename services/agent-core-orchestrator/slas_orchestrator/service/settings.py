"""Process settings: plain environment variables, read once at `serve` (contract §1).

Facts only — the orchestrator holds no secret. The URLs name the other containers on the
backend network; the two files are the glossary the SOP renderer pins (§5.5) and the owner
routing table the RCA pipeline uses (§5.4), both mounted read-only by compose.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from slas_http.serve import DEFAULT_BIND

SERVICE_NAME: Final = "agent-core-orchestrator"

DEFAULTS: Final[dict[str, str]] = {
    "SLAS_GATEWAY_URL": "http://llm-gateway:8000",
    "SLAS_SANDBOX_MANAGER_URL": "http://sandbox-manager:8000",
    "SLAS_GIT_BROKER_URL": "http://git-broker:8000",
    "SLAS_VALIDATION_EXECUTOR_URL": "http://validation-executor:8000",
    "SLAS_FACTORY_EXECUTOR_URL": "http://factory-executor:8000",
    "SLAS_DATA_ROOT": "/data",
    "SLAS_BIND": DEFAULT_BIND,
    "SLAS_GLOSSARY": "/etc/slas/glossary.yaml",
    "SLAS_OWNER_ROUTING": "/etc/slas/owner-routing.yaml",
}


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    sandbox_manager_url: str
    git_broker_url: str
    validation_executor_url: str
    factory_executor_url: str
    data_root: Path
    bind: str
    glossary_file: Path
    owner_routing_file: Path
    #: Seconds a health probe waits for another service (contract §5).
    probe_timeout_s: float = 2.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env

        def read(name: str) -> str:
            value = source.get(name, "").strip()
            return value or DEFAULTS[name]

        return cls(
            gateway_url=read("SLAS_GATEWAY_URL"),
            sandbox_manager_url=read("SLAS_SANDBOX_MANAGER_URL"),
            git_broker_url=read("SLAS_GIT_BROKER_URL"),
            validation_executor_url=read("SLAS_VALIDATION_EXECUTOR_URL"),
            factory_executor_url=read("SLAS_FACTORY_EXECUTOR_URL"),
            data_root=Path(read("SLAS_DATA_ROOT")),
            bind=read("SLAS_BIND"),
            glossary_file=Path(read("SLAS_GLOSSARY")),
            owner_routing_file=Path(read("SLAS_OWNER_ROUTING")),
        )

    @property
    def skills_library(self) -> Path:
        return self.data_root / "Skills" / "library"

    def service_urls(self) -> dict[str, str]:
        """Health-check name → base URL, in the order the contract lists them."""
        return {
            "gateway": self.gateway_url,
            "sandbox_manager": self.sandbox_manager_url,
            "validation_executor": self.validation_executor_url,
            "factory_executor": self.factory_executor_url,
            "git_broker": self.git_broker_url,
        }


#: The checks whose failure makes the orchestrator unhealthy; the rest are optional zones.
MANDATORY_CHECKS: Final[frozenset[str]] = frozenset({"gateway", "sandbox_manager"})
