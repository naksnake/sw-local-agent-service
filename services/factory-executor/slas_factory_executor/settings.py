"""Factory settings rendered to `config/factory.yaml`: screenshot retention, enrolment code
rules, the VNC port stations expose. Read once by the executor; a test keeps file and code
in step. Model and user changes never need an edit here (INV-9)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import Field, ValidationError

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_screen.retention import PruneReport, RetentionPolicy, prune_screenshots


class FactorySettings(SlasModel):
    version: int = 1
    screenshot_retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    enrolment_code_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    enrolment_max_attempts: int = Field(default=5, ge=1, le=100)
    vnc_port: int = Field(default=5900, ge=1, le=65535)
    station_lease_hours: int = Field(default=8, ge=1, le=72)

    def sentences(self) -> list[str]:
        return [
            self.screenshot_retention.sentence(),
            f"An enrolment code works once, within {self.enrolment_code_ttl_minutes} minutes; "
            f"{self.enrolment_max_attempts} wrong codes lock the station until a new code "
            "is issued.",
            f"Stations expose VNC on port {self.vnc_port}, relayed to the operator over the "
            "runner's mTLS channel.",
            f"A station lease lasts at most {self.station_lease_hours} hours.",
        ]


class FactorySettingsError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def settings_from_mapping(data: object, *, source: str = "<memory>") -> FactorySettings:
    try:
        return FactorySettings.model_validate(data)
    except ValidationError as exc:
        raise FactorySettingsError(
            ThreePartMessage(
                f"The factory settings in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; every value is a positive number.",
            )
        ) from exc


DEFAULT_FACTORY_SETTINGS: Final[dict[str, object]] = {
    "version": 1,
    "screenshot_retention": {"keep_days": 30, "keep_failed_days": 180, "max_per_job": 400},
    "enrolment_code_ttl_minutes": 15,
    "enrolment_max_attempts": 5,
    "vnc_port": 5900,
    "station_lease_hours": 8,
}

FACTORY_FILE_HEADER: Final = (
    "Factory settings for SW Local Agent Service (CLAUDE.md §10.3, P10).\n"
    "Rendered from slas_factory_executor.settings.DEFAULT_FACTORY_SETTINGS; a unit test keeps\n"
    "file and code in step. Screenshot retention applies to Factory/Jobs/<ticket>/screens on the\n"
    "platform and, per station, to the runner's own copies."
)


def default_settings() -> FactorySettings:
    return settings_from_mapping(DEFAULT_FACTORY_SETTINGS, source="config/factory.yaml")


def render_factory_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    s = settings_from_mapping(data)
    lines = [f"# {line}".rstrip() for line in header.splitlines()] if header else []
    lines += [
        f"version: {s.version}",
        "screenshot_retention:",
        f"  keep_days: {s.screenshot_retention.keep_days}",
        f"  keep_failed_days: {s.screenshot_retention.keep_failed_days}",
        f"  max_per_job: {s.screenshot_retention.max_per_job}",
        f"enrolment_code_ttl_minutes: {s.enrolment_code_ttl_minutes}",
        f"enrolment_max_attempts: {s.enrolment_max_attempts}",
        f"vnc_port: {s.vnc_port}",
        f"station_lease_hours: {s.station_lease_hours}",
    ]
    return "\n".join(lines) + "\n"


def prune_job_screenshots(
    data_root: Path, policy: RetentionPolicy, *, now: datetime, held_or_failed: Iterable[str] = ()
) -> PruneReport:
    """Prune `Factory/Jobs/<ticket>/screens/*.png`; held or failed tickets keep theirs longer."""
    return prune_screenshots(
        data_root / "Factory" / "Jobs", policy, now=now, failed_jobs=held_or_failed
    )


def dump_settings(settings: FactorySettings) -> str:
    return json.dumps(settings.model_dump(mode="json"), indent=2)
