"""Process settings for the api (ADR-0005): environment for facts, files for secrets.

Everything the container knows arrives as an environment variable or a file under
`/run/secrets`. Passwords never sit in an environment variable: the database URL and the
Redis connection are assembled in memory from `postgres_password` and `redis_password`
(INV-5). Tests point `SLAS_SECRETS_DIR` at a temporary directory and `SLAS_DATABASE_URL` at
a SQLite file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, make_url

Argon2Profile = Literal["production", "test"]

#: File names under the secrets directory (compose/docker-compose.yml `secrets:`).
POSTGRES_PASSWORD_SECRET = "postgres_password"  # noqa: S105 — a file name, not a password
REDIS_PASSWORD_SECRET = "redis_password"  # noqa: S105
SECRET_KEY_SECRET = "secret_key"  # noqa: S105
ADMIN_INITIAL_PASSWORD_SECRET = "admin-initial-password"  # noqa: S105


class Settings(BaseSettings):
    """Read once at start. Field names are the environment variable names, lowercased."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    slas_data_root: Path = Path("/data")
    slas_profile: str = "quickstart"
    slas_roles_file: Path = Path("/etc/slas/rbac-roles.yaml")
    slas_auth_modes: str = "builtin"
    slas_version: str = ""
    slas_https_port: str = ""
    slas_tls_mode: str = ""
    slas_tls_names: str = ""

    postgres_db: str = "slas"
    postgres_user: str = "slas"
    slas_postgres_host: str = "postgres"
    slas_postgres_port: int = 5432
    slas_redis_host: str = "redis"
    slas_redis_port: int = 6379

    slas_secrets_dir: Path = Path("/run/secrets")
    slas_database_url: str = Field(
        default="",
        description="Overrides the assembled Postgres URL; tests use sqlite+pysqlite:///…",
    )

    slas_argon2_profile: Argon2Profile = "production"
    slas_cookie_secure: bool = True
    slas_bind: str = "0.0.0.0:8000"  # the container's own interface

    # The services the browser-facing routes proxy to (docs/api-contract-round-2.md §1).
    # Defaults are the compose service names on the slas-backend network.
    slas_orchestrator_url: str = "http://agent-core-orchestrator:8000"
    slas_git_broker_url: str = "http://git-broker:8000"
    slas_sandbox_manager_url: str = "http://sandbox-manager:8000"
    slas_factory_executor_url: str = "http://factory-executor:8000"
    slas_model_manager_url: str = "http://model-manager:8000"

    # --- derived ------------------------------------------------------------------------

    def auth_modes(self) -> list[str]:
        return [mode.strip() for mode in self.slas_auth_modes.split(",") if mode.strip()]

    def read_secret(self, name: str) -> str | None:
        """The content of one secret file, stripped, or None when the file is absent."""
        path = self.slas_secrets_dir / name
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def database_url(self) -> URL:
        """A SQLAlchemy URL object; the password lives inside it, never in a string we log."""
        if self.slas_database_url:
            return make_url(self.slas_database_url)
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.read_secret(POSTGRES_PASSWORD_SECRET),
            host=self.slas_postgres_host,
            port=self.slas_postgres_port,
            database=self.postgres_db,
        )

    def redis_password(self) -> str | None:
        return self.read_secret(REDIS_PASSWORD_SECRET)

    @property
    def env_file(self) -> Path:
        return self.slas_data_root / ".env"

    @property
    def models_file(self) -> Path:
        return self.slas_data_root / "Models" / "models.yaml"

    @property
    def models_dir(self) -> Path:
        return self.slas_data_root / "Models"
