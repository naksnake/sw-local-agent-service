"""`slas-station-runner` on a station (P10): enrol, doctor, show, windows, prune, serve."""

from __future__ import annotations

import io
import json
import platform
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_factory_executor.stations import (
    EnrolmentService,
    FakeCa,
    StationError,
    StationRecord,
    StationRegistry,
)
from slas_station_runner import cli
from slas_station_runner.enrol import RunnerSettings, load_settings
from slas_station_runner.protocol import EnrolmentRequest
from slas_station_runner.runner import VncSettings

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class FakePoster:
    def __init__(self, svc: EnrolmentService) -> None:
        self.svc = svc

    def post(self, url: str, body: bytes, *, cafile: str) -> tuple[int, bytes]:
        try:
            grant = self.svc.redeem(EnrolmentRequest.model_validate_json(body))
        except StationError as exc:
            return 403, json.dumps(exc.message.as_dict()).encode()
        return 200, grant.model_dump_json().encode()


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(list(argv), stdout=out)
    return code, out.getvalue()


@pytest.fixture
def enrolled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = StationRegistry(tmp_path / "Factory" / "stations.json")
    registry.put(
        StationRecord(
            name="station-07", allowed_programs=["burnin-ctl"], vnc=VncSettings(enabled=True)
        )
    )
    svc = EnrolmentService(registry, ca=FakeCa(), clock=Clock(), data_root=tmp_path)
    monkeypatch.setattr(cli, "UrllibPoster", lambda: FakePoster(svc))
    code = svc.issue("station-07", by="admin").code
    ca = tmp_path / "slas-ca.pem"
    ca.write_text("bundle ca")
    state = tmp_path / "state"
    status, out = run(
        "--state-dir",
        str(state),
        "enrol",
        "--platform",
        "https://executor:8444",
        "--station",
        "station-07",
        "--code",
        code,
        "--runner-url",
        "https://127.0.0.1:8443",
        "--ca",
        str(ca),
        "--bind",
        "127.0.0.1:0",
    )
    assert status == 0, out
    assert out.startswith("station-07 is enrolled: certificate SHA256:")
    assert "Next: `slas-station-runner doctor`, then `slas-station-runner serve`." in out
    # a second attempt with the used code is refused in three parts, nothing overwritten
    status, out = run(
        "--state-dir",
        str(tmp_path / "again"),
        "enrol",
        "--platform",
        "https://executor:8444",
        "--station",
        "station-07",
        "--code",
        code,
        "--runner-url",
        "https://127.0.0.1:8443",
        "--ca",
        str(ca),
    )
    assert status == 1 and "No enrolment code is open for station-07." in out
    return state


def test_no_command_prints_help(tmp_path: Path) -> None:
    status, out = run("--state-dir", str(tmp_path))
    assert status == 2 and "enrol" in out and "doctor" in out


def test_doctor_names_every_missing_file_before_enrolment(tmp_path: Path) -> None:
    status, out = run("--state-dir", str(tmp_path / "empty"), "doctor", "--screen-backend", "fake")
    assert status == 1
    for name in ("runner.json", "config.json", "ca.pem", "client.pem", "client.key", "batch.key"):
        assert f"FAIL {name} is missing under" in out
    assert "Summary: 6 problems must be fixed before serving." in out


def test_doctor_show_windows_prune_and_serve_once_on_an_enrolled_station(enrolled: Path) -> None:
    status, out = run("--state-dir", str(enrolled), "doctor", "--screen-backend", "fake")
    assert status == 0, out
    assert "ok   client.key is mode 600" in out
    assert "ok   GUI backend: the fake screen (smoke test only)" in out
    assert "Windows matched by contains" in out
    assert "Screenshots are kept 30 days" in out
    assert "Summary: the station is ready to serve." in out
    assert "xdotool" not in out, "tool checks belong to the xdotool backend, not the fake"
    assert "VNC on port 5900 is relayed to the operator" in out

    status, out = run("--state-dir", str(enrolled), "show")
    assert status == 0
    assert out.splitlines()[0] == (
        "station-07 serves at https://127.0.0.1:8443 (bound to 127.0.0.1:0), enrolled with "
        f"https://executor:8444; certificate {load_settings(enrolled).cert_fingerprint}."
    )
    assert "Allowed programs: burnin-ctl." in out and "VNC: on, port 5900." in out
    assert "FAKE-KEY" not in out and (enrolled / "batch.key").read_text().strip() not in out

    status, out = run("--state-dir", str(enrolled), "windows", "--screen-backend", "fake")
    assert status == 0 and out == "No window is visible (the fake screen (smoke test only)).\n"

    status, out = run("--state-dir", str(enrolled), "prune")
    assert status == 0 and out == "Looked at 0 screenshots in 0 jobs: deleted 0, kept 0.\n"

    status, out = run("--state-dir", str(enrolled), "serve", "--screen-backend", "fake", "--once")
    assert status == 1, "the fake CA mints no real certificate, so the TLS server refuses it"
    assert "The runner could not start its TLS server on 127.0.0.1:0." in out


def test_serve_and_show_explain_a_missing_enrolment(tmp_path: Path) -> None:
    for command in ("serve", "show", "windows", "prune"):
        status, out = run("--state-dir", str(tmp_path / "none"), command)
        assert status == 1, command
        assert f"{tmp_path / 'none'} holds no enrolled runner." in out


def test_choose_backend_prefers_the_asked_for_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = RunnerSettings(
        station="s",
        platform_url="https://p",
        runner_url="https://r",
        batch_key_id="k",
        screen_backend="fake",
    )
    backend, how = cli.choose_backend(settings)
    assert how == "the fake screen (smoke test only)"
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    backend, how = cli.choose_backend(settings.model_copy(update={"screen_backend": "auto"}))
    assert how == "no GUI backend on this station"
    assert isinstance(backend, cli.NoScreenBackend)
    assert backend.windows() == [] and backend.focused() is None
    with pytest.raises(RuntimeError, match="PyAutoGUI"):
        backend.type_text("x")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    _, how = cli.choose_backend(settings.model_copy(update={"screen_backend": "auto"}))
    assert how == "no GUI backend on this station"
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/xdotool")
    monkeypatch.setenv("DISPLAY", ":1")
    _, how = cli.choose_backend(settings.model_copy(update={"screen_backend": "auto"}))
    assert how == "xdotool on :1"
