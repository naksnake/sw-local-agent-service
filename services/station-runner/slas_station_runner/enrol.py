"""The station side of enrolment (P10): one call with the one-time code, then the station's
identity and configuration land in its state directory, mode 0600, and the runner can serve.

    <state_dir>/ca.pem          the platform CA (verifies the executor)
    <state_dir>/client.pem      the station's certificate, minted by the platform CA
    <state_dir>/client.key      its private key
    <state_dir>/batch.key       the HMAC key batches are signed with
    <state_dir>/config.json     the StationConfig the administrator wrote
    <state_dir>/runner.json     where the platform is, what this runner is called, how to bind
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_station_runner.protocol import BatchError, EnrolmentGrant, EnrolmentRequest, three_part
from slas_station_runner.runner import StationConfig


class RunnerSettings(SlasModel):
    """`runner.json`: everything the runner needs at start besides the secret files."""

    station: str = Field(min_length=1)
    platform_url: str = Field(pattern=r"^https://")
    runner_url: str = Field(pattern=r"^https://")
    bind: str = Field(default="0.0.0.0:8443", min_length=3)
    batch_key_id: str = Field(min_length=1)
    cert_fingerprint: str = ""
    #: fake · xdotool · none — chosen by `doctor`/`serve` for this OS; `none` refuses GUI steps.
    screen_backend: str = "auto"
    display: str = ":0"

    def sentence(self) -> str:
        return (
            f"{self.station} serves at {self.runner_url} (bound to {self.bind}), enrolled with "
            f"{self.platform_url}; certificate {self.cert_fingerprint or '?'}."
        )


class Poster(Protocol):
    def post(self, url: str, body: bytes, *, cafile: str) -> tuple[int, bytes]: ...


class UrllibPoster:
    def post(self, url: str, body: bytes, *, cafile: str) -> tuple[int, bytes]:
        context = ssl.create_default_context(cafile=cafile)
        request = urllib.request.Request(  # noqa: S310 — https, CA pinned
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310
                return int(response.status), bytes(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise BatchError(
                ThreePartMessage(
                    f"The platform at {url} did not answer.",
                    f"{exc.reason if isinstance(exc, urllib.error.URLError) else exc}",
                    "Check the address, that the station reaches the factory network, and that "
                    "slas-ca.pem in the bundle is the platform's CA.",
                )
            ) from None


class EnrolResult(SlasModel):
    settings: RunnerSettings
    files: list[str]
    sentence: str


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def enrol(
    *,
    platform_url: str,
    station: str,
    code: str,
    runner_url: str,
    state_dir: Path,
    cafile: Path,
    poster: Poster,
    bind: str = "0.0.0.0:8443",
) -> EnrolResult:
    request = EnrolmentRequest(station=station, code=code, runner_url=runner_url)
    status, body = poster.post(
        f"{platform_url.rstrip('/')}/enrol",
        request.model_dump_json().encode("utf-8"),
        cafile=str(cafile),
    )
    if status != 200:
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        raise BatchError(three_part(payload if isinstance(payload, dict) else {}))
    grant = EnrolmentGrant.model_validate_json(body)
    if grant.station != station:
        raise BatchError(
            ThreePartMessage(
                f"The platform answered for {grant.station}, not for {station}.",
                "The grant does not match the station that asked.",
                "Nothing was written. Check the station name and try again.",
            )
        )
    config = StationConfig.model_validate(grant.config)
    files = {
        "ca.pem": grant.ca_pem,
        "client.pem": grant.client_cert_pem,
        "client.key": grant.client_key_pem,
        "batch.key": grant.batch_key + "\n",
    }
    for name, content in files.items():
        _write_private(state_dir / name, content)
    settings = RunnerSettings(
        station=station,
        platform_url=platform_url,
        runner_url=runner_url,
        bind=bind,
        batch_key_id=grant.batch_key_id,
        cert_fingerprint=grant.cert_fingerprint,
    )
    write_atomic(state_dir / "config.json", config.model_dump_json(indent=2) + "\n", mode=0o600)
    write_atomic(state_dir / "runner.json", settings.model_dump_json(indent=2) + "\n", mode=0o600)
    return EnrolResult(
        settings=settings,
        files=sorted([*files, "config.json", "runner.json"]),
        sentence=f"{grant.sentence} Files written under {state_dir}.",
    )


def load_settings(state_dir: Path) -> RunnerSettings:
    path = state_dir / "runner.json"
    if not path.is_file():
        raise BatchError(
            ThreePartMessage(
                f"{state_dir} holds no enrolled runner.",
                "runner.json is missing; the station was never enrolled or the directory is wrong.",
                "Run `slas-station-runner enrol --platform … --station … --code …` first.",
            )
        )
    return RunnerSettings.model_validate_json(path.read_text(encoding="utf-8"))


def load_config(state_dir: Path) -> StationConfig:
    return StationConfig.model_validate_json(
        (state_dir / "config.json").read_text(encoding="utf-8")
    )


def load_batch_key(state_dir: Path) -> bytes:
    return (state_dir / "batch.key").read_text(encoding="utf-8").strip().encode("utf-8")
