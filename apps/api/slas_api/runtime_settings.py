"""Runtime settings in Postgres, mirrored into `${SLAS_DATA_ROOT}/.env` (ADR-0008).

Three keys in Phase 1: `installation_name`, `chinese_variant`, `session_lifetime_hours`.
The database wins; `.env` seeds the table only while it is empty; every change and every
start rewrites just these keys under one marker with `slas_schemas.envfile`, so comments,
order and every other line stay byte for byte. A mirror failure never blocks a save.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from slas_api.errors import ApiError
from slas_api.models import Setting
from slas_api.settings import Settings
from slas_schemas.envfile import EnvFile, read_env, write_atomic
from slas_schemas.errors import ThreePartMessage

MARKER: Final = "# managed by Admin → Settings"
NAME_LIMIT: Final = 60
HOURS_MIN: Final = 1
HOURS_MAX: Final = 168
ChineseVariant = Literal["zh-Hant", "zh-Hans"]
_VARIANTS: Final = ("zh-Hant", "zh-Hans")

Normaliser = Callable[[object], "str | ThreePartMessage"]


def _name(value: object) -> str | ThreePartMessage:
    if not isinstance(value, str):
        return ThreePartMessage(
            "The name has to be text.",
            "The page sent something other than text.",
            "Type a name, for example Lab 3.",
        )
    name = value.strip()
    if not name:
        return ThreePartMessage(
            "The name is empty.",
            "Everything was deleted from the field.",
            "Type a name, for example Lab 3.",
        )
    if len(name) > NAME_LIMIT:
        return ThreePartMessage(
            f"The name is too long: it has {len(name)} characters, the limit is {NAME_LIMIT}.",
            "The sign-in page has room for a short name.",
            "Shorten it.",
        )
    return name


def _variant(value: object) -> str | ThreePartMessage:
    if isinstance(value, str) and value in _VARIANTS:
        return value
    return ThreePartMessage(
        f"The Chinese variant {value!r} isn't one the platform knows.",
        "The choices are zh-Hant (Traditional) and zh-Hans (Simplified).",
        "Pick one of them.",
    )


def _hours(value: object) -> str | ThreePartMessage:
    hours: int | None = None
    if isinstance(value, bool):
        hours = None
    elif isinstance(value, int):
        hours = value
    elif isinstance(value, str) and value.strip().isdigit():
        hours = int(value.strip())
    if hours is None or not HOURS_MIN <= hours <= HOURS_MAX:
        shown = hours if hours is not None else value
        return ThreePartMessage(
            f"A sign-in can't last {shown!s} hours: the range is {HOURS_MIN} to {HOURS_MAX}.",
            "The choices are 8 hours, 1 day or 7 days.",
            "Pick a value between 1 hour and 7 days.",
        )
    return str(hours)


@dataclass(frozen=True, slots=True)
class RuntimeKey:
    name: str
    env_key: str
    default: str
    normalise: Normaliser


RUNTIME_KEYS: Final[dict[str, RuntimeKey]] = {
    key.name: key
    for key in (
        RuntimeKey("installation_name", "SLAS_INSTALLATION_NAME", "SW Local Agent Service", _name),
        RuntimeKey("chinese_variant", "SLAS_SOP_CHINESE", "zh-Hant", _variant),
        RuntimeKey("session_lifetime_hours", "SLAS_SESSION_LIFETIME_HOURS", "8", _hours),
    )
}


class RuntimeValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_name: str
    chinese_variant: ChineseVariant
    session_lifetime_hours: int

    @classmethod
    def from_strings(cls, values: Mapping[str, str]) -> RuntimeValues:
        merged = {name: values.get(name, key.default) for name, key in RUNTIME_KEYS.items()}
        return cls(
            installation_name=merged["installation_name"],
            chinese_variant="zh-Hans" if merged["chinese_variant"] == "zh-Hans" else "zh-Hant",
            session_lifetime_hours=int(merged["session_lifetime_hours"]),
        )

    def as_strings(self) -> dict[str, str]:
        return {
            "installation_name": self.installation_name,
            "chinese_variant": self.chinese_variant,
            "session_lifetime_hours": str(self.session_lifetime_hours),
        }


def _rows(db: Session) -> dict[str, Setting]:
    rows = db.scalars(select(Setting).where(Setting.key.in_(list(RUNTIME_KEYS)))).all()
    return {row.key: row for row in rows}


def read_runtime(db: Session) -> RuntimeValues:
    """Current values; a key missing from the table reads as its default."""
    return RuntimeValues.from_strings({key: row.value for key, row in _rows(db).items()})


def seed_if_empty(db: Session, env_path: Path, now: datetime) -> bool:
    """Fill an empty table from `.env` (valid values) and the defaults. Returns True if it did."""
    if _rows(db):
        return False
    env = _read_env_or_none(env_path)
    for name, key in RUNTIME_KEYS.items():
        value = key.default
        if env is not None:
            raw = env.get(key.env_key)
            if raw is not None:
                normalised = key.normalise(raw)
                if isinstance(normalised, str):
                    value = normalised
        db.add(Setting(key=name, value=value, updated_at=now))
    db.flush()
    return True


def apply_changes(db: Session, changes: Mapping[str, object], now: datetime) -> RuntimeValues:
    """Validate and write a subset of the runtime keys; 400 in three parts on a bad value."""
    unknown = sorted(set(changes) - set(RUNTIME_KEYS))
    if unknown:
        raise ApiError.build(
            400,
            f"There is no setting called {unknown[0]}.",
            f"The settings you can change are {', '.join(RUNTIME_KEYS)}.",
            "Reload the Settings page and try again.",
        )
    normalised: dict[str, str] = {}
    for name, value in changes.items():
        result = RUNTIME_KEYS[name].normalise(value)
        if isinstance(result, ThreePartMessage):
            raise ApiError(400, result)
        normalised[name] = result
    rows = _rows(db)
    for name, value in normalised.items():
        row = rows.get(name)
        if row is None:
            db.add(Setting(key=name, value=value, updated_at=now))
        elif row.value != value:
            row.value = value
            row.updated_at = now
    db.flush()
    return read_runtime(db)


def _read_env_or_none(env_path: Path) -> EnvFile | None:
    try:
        return read_env(env_path)
    except OSError:
        return None


def mirror_to_env(values: RuntimeValues, env_path: Path) -> ThreePartMessage | None:
    """Rewrite only the runtime keys under the marker. None on success, the notice otherwise."""
    try:
        exists = env_path.exists()
        env = read_env(env_path) if exists else EnvFile()
        mode = (env_path.stat().st_mode & 0o777) if exists else 0o600
        changed = False
        for name, value in values.as_strings().items():
            changed |= env.set(RUNTIME_KEYS[name].env_key, value, under_marker=MARKER)
        if changed:
            write_atomic(env_path, env.render(), mode=mode)
    except OSError:
        return mirror_failure_notice(env_path)
    return None


def mirror_failure_notice(env_path: Path) -> ThreePartMessage:
    host_root = None
    env = _read_env_or_none(env_path)
    if env is not None:
        host_root = env.get("SLAS_DATA_ROOT")
    shown = f"{host_root.rstrip('/')}/.env" if host_root else str(env_path)
    return ThreePartMessage(
        "Saved, but the copy in `.env` couldn't be written.",
        "The data root isn't writable by the api service.",
        f"The settings apply now; fix permissions on {shown} so they survive a reinstall.",
    )


def notice_sentence(message: ThreePartMessage | None) -> str | None:
    if message is None:
        return None
    return f"{message.what_happened} {message.likely_cause} {message.what_to_do}"


class InstallFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    data_root: str
    https_port: str
    tls_mode: str
    tls_names: list[str]
    version: str


def install_facts(settings: Settings, fallback_version: str) -> InstallFacts:
    """Set at install, read-only in the UI. The host's `.env` names the host paths.

    Assumption: the container sees the data root at /data, so `data_root` prefers the value
    written in `.env` (the host path an operator would type); the other facts prefer the
    container's environment and fall back to `.env`, then to the quickstart defaults.
    """
    env = _read_env_or_none(settings.env_file)

    def from_env(key: str) -> str:
        if env is None:
            return ""
        return (env.get(key) or "").strip()

    names = settings.slas_tls_names or from_env("SLAS_TLS_NAMES")
    return InstallFacts(
        profile=settings.slas_profile or from_env("SLAS_PROFILE") or "quickstart",
        data_root=from_env("SLAS_DATA_ROOT") or str(settings.slas_data_root),
        https_port=settings.slas_https_port or from_env("SLAS_HTTPS_PORT") or "443",
        tls_mode=settings.slas_tls_mode or from_env("SLAS_TLS_MODE") or "self-signed",
        tls_names=[part.strip() for part in names.replace(",", " ").split() if part.strip()],
        version=settings.slas_version or from_env("SLAS_VERSION") or fallback_version,
    )


def detail_json(values: Mapping[str, object]) -> str:
    return json.dumps(dict(values), ensure_ascii=False, sort_keys=True, default=str)
