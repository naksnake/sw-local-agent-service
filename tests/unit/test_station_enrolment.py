"""Admin → Stations enrolment (P10): a one-time code becomes a station's mTLS identity.

Against the fake CA: issue, redeem, wrong codes lock, expiry, single use, revoke. Against a
real platform CA driven through the `openssl` binary: the enrolment endpoint over TLS, then
the enrolled files serve a runner that the executor reaches with its own certificate.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slas_factory_executor.stations import (
    CODE_ALPHABET,
    EnrolmentServer,
    EnrolmentService,
    FakeCa,
    OpensslCa,
    StationError,
    StationRecord,
    StationRegistry,
    copy_ca_for_bundle,
    hash_code,
    new_code,
    normalise_code,
    san_entries,
)
from slas_hal.credentials import LocalCredentialResolver
from slas_hal.drivers.process import LocalProcessRunner
from slas_station_runner import cli
from slas_station_runner.enrol import (
    UrllibPoster,
    enrol,
    load_batch_key,
    load_config,
    load_settings,
)
from slas_station_runner.protocol import (
    BatchError,
    EnrolmentRequest,
    StepBatch,
    new_batch_id,
    sign_batch,
)
from slas_station_runner.runner import ScreenTuning, VncSettings
from slas_station_runner.server import MtlsRunnerClient, RunnerServer

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
STATION = "station-07"


class TickingClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        self.current += timedelta(**kwargs)


def service(tmp_path: Path, clock: TickingClock | None = None) -> tuple[EnrolmentService, FakeCa]:
    registry = StationRegistry(tmp_path / "Factory" / "stations.json")
    registry.put(
        StationRecord(
            name=STATION,
            description="Final test, line 2",
            allowed_programs=["burnin-ctl", "fixture-ctl"],
            screen=ScreenTuning(window_match="prefix", action_settle_s=0.3),
            vnc=VncSettings(enabled=True),
        )
    )
    ca = FakeCa()
    return (
        EnrolmentService(registry, ca=ca, clock=clock or TickingClock(), data_root=tmp_path),
        ca,
    )


# --- codes ---------------------------------------------------------------------------------------


def test_codes_are_readable_aloud_and_compared_normalised() -> None:
    for _ in range(50):
        code = new_code()
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code), code
        assert not set(code.replace("-", "")) - set(CODE_ALPHABET)
    assert normalise_code(" abcd-efgh 2345 ") == "ABCDEFGH2345"
    assert hash_code("salt", "ABCD-EFGH-2345") == hash_code("salt", "abcdefgh2345")
    assert hash_code("salt", "ABCD-EFGH-2345") != hash_code("other", "ABCD-EFGH-2345")


def test_issue_then_redeem_enrols_the_station_once(tmp_path: Path) -> None:
    svc, ca = service(tmp_path)
    assert svc.registry.get(STATION).sentence() == "station-07: not enrolled yet."

    issued = svc.issue(STATION, by="admin")
    assert issued.sentence == (
        f"Enter this code on station-07 within 15 minutes: {issued.code}. It works once; "
        "issuing a new code cancels it."
    )
    stored = svc.registry.code_for(STATION)
    assert stored is not None and issued.code not in stored.model_dump_json(), "hashed, not kept"

    request = EnrolmentRequest(
        station=STATION, code=issued.code.lower(), runner_url="https://10.20.0.7:8443"
    )
    grant = svc.redeem(request)
    assert grant.station == STATION and grant.ca_pem == ca.ca_pem()
    assert grant.cert_fingerprint.startswith("SHA256:")
    assert ca.sans == ["IP:10.20.0.7,DNS:station-07"], "valid as the runner's server cert too"
    assert grant.config["screen"]["window_match"] == "prefix"
    assert grant.config["allowed_programs"] == ["burnin-ctl", "fixture-ctl"]
    assert grant.sentence.startswith("station-07 is enrolled: certificate SHA256:")

    key_path = tmp_path / "Factory" / "keys" / "station-07.key"
    assert key_path.read_text().strip() == grant.batch_key
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert svc.batch_key_ref(STATION) == "file:Factory/keys/station-07.key"
    resolver = LocalCredentialResolver(root=tmp_path)
    assert resolver.resolve(svc.batch_key_ref(STATION)) == grant.batch_key

    record = svc.registry.get(STATION)
    assert record.enrolled and record.batch_key_id == grant.batch_key_id
    assert record.sentence() == (
        "station-07: enrolled 2026-09-14 10:00, runner at https://10.20.0.7:8443, certificate "
        f"{grant.cert_fingerprint}."
    )

    with pytest.raises(StationError) as used:
        svc.redeem(request)
    assert used.value.message.what_happened == "No enrolment code is open for station-07."
    assert used.value.message.likely_cause == "None was issued, or the last one was already used."


def test_wrong_codes_count_down_and_lock_until_a_new_code_is_issued(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    issued = svc.issue(STATION, by="admin")
    wrong = EnrolmentRequest(station=STATION, code="AAAA-BBBB-CCCC", runner_url="https://s7")
    for left in (4, 3, 2, 1):
        with pytest.raises(StationError) as exc:
            svc.redeem(wrong)
        assert exc.value.message.what_happened == "The code for station-07 is not right."
        assert exc.value.message.likely_cause == (
            f"{left} {'attempt' if left == 1 else 'attempts'} left before enrolment locks."
        )
    with pytest.raises(StationError) as exc:
        svc.redeem(wrong)
    assert exc.value.message.likely_cause == "0 attempts left before enrolment locks."
    right = EnrolmentRequest(station=STATION, code=issued.code, runner_url="https://s7")
    with pytest.raises(StationError) as locked:
        svc.redeem(right)
    assert locked.value.message.what_happened == (
        "Enrolment of station-07 is locked after 5 wrong codes."
    )
    fresh = svc.issue(STATION, by="admin")
    assert fresh.code != issued.code
    grant = svc.redeem(EnrolmentRequest(station=STATION, code=fresh.code, runner_url="https://s7"))
    assert grant.station == STATION, "a new code unlocks the station"


def test_codes_expire_and_a_new_one_cancels_the_old(tmp_path: Path) -> None:
    clock = TickingClock()
    svc, _ = service(tmp_path, clock)
    first = svc.issue(STATION, by="admin")
    second = svc.issue(STATION, by="admin")
    with pytest.raises(StationError) as exc:
        svc.redeem(EnrolmentRequest(station=STATION, code=first.code, runner_url="https://s7"))
    assert exc.value.message.what_happened == "The code for station-07 is not right."
    clock.advance(minutes=16)
    with pytest.raises(StationError) as expired:
        svc.redeem(EnrolmentRequest(station=STATION, code=second.code, runner_url="https://s7"))
    assert expired.value.message.what_happened == (
        "The enrolment code for station-07 expired at 10:15."
    )
    assert svc.registry.code_for(STATION) is None, "an expired code is dropped"
    with pytest.raises(StationError) as unknown:
        svc.issue("station-99", by="admin")
    assert unknown.value.message.what_happened == "There is no station called station-99."


def test_revoke_clears_the_identity_and_the_key_file(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    issued = svc.issue(STATION, by="admin")
    svc.redeem(EnrolmentRequest(station=STATION, code=issued.code, runner_url="https://s7"))
    key_path = tmp_path / "Factory" / "keys" / "station-07.key"
    assert key_path.exists()
    record = svc.revoke(STATION)
    assert not record.enrolled and record.cert_fingerprint is None
    assert not key_path.exists() and svc.registry.code_for(STATION) is None
    assert svc.registry.remove(STATION) is True and svc.registry.list() == []
    assert svc.registry.remove(STATION) is False
    assert svc.registry.path.stat().st_mode & 0o777 == 0o600


# --- the station side ---------------------------------------------------------------------------


class InProcessPoster:
    """Calls the service directly: the wire format without the wire."""

    def __init__(self, svc: EnrolmentService) -> None:
        self.svc = svc
        self.urls: list[str] = []

    def post(self, url: str, body: bytes, *, cafile: str) -> tuple[int, bytes]:
        self.urls.append(url)
        request = EnrolmentRequest.model_validate_json(body)
        try:
            grant = self.svc.redeem(request)
        except StationError as exc:
            import json

            return 403, json.dumps(exc.message.as_dict()).encode()
        return 200, grant.model_dump_json().encode()


def test_enrol_writes_the_identity_files_privately(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    issued = svc.issue(STATION, by="admin")
    poster = InProcessPoster(svc)
    state = tmp_path / "runner-state"
    cafile = tmp_path / "slas-ca.pem"
    cafile.write_text("ca")
    result = enrol(
        platform_url="https://executor.factory.internal:8444/",
        station=STATION,
        code=issued.code,
        runner_url="https://station-07.factory.internal:8443",
        state_dir=state,
        cafile=cafile,
        poster=poster,
        bind="0.0.0.0:8443",
    )
    assert poster.urls == ["https://executor.factory.internal:8444/enrol"]
    assert result.files == [
        "batch.key",
        "ca.pem",
        "client.key",
        "client.pem",
        "config.json",
        "runner.json",
    ]
    for name in result.files:
        assert (state / name).stat().st_mode & 0o777 == 0o600, name
    assert result.sentence.endswith(f"Files written under {state}.")
    settings = load_settings(state)
    assert settings.sentence() == (
        "station-07 serves at https://station-07.factory.internal:8443 (bound to 0.0.0.0:8443), "
        f"enrolled with https://executor.factory.internal:8444/; certificate "
        f"{settings.cert_fingerprint}."
    )
    assert load_config(state).screen.action_settle_s == 0.3
    assert load_batch_key(state) == (tmp_path / "Factory/keys/station-07.key").read_bytes().strip()

    with pytest.raises(BatchError) as again:
        enrol(
            platform_url="https://executor.factory.internal:8444",
            station=STATION,
            code=issued.code,
            runner_url="https://station-07.factory.internal:8443",
            state_dir=tmp_path / "other",
            cafile=cafile,
            poster=poster,
        )
    assert again.value.message.what_happened == "No enrolment code is open for station-07."
    assert not (tmp_path / "other").exists(), "a refused enrolment writes nothing"

    with pytest.raises(BatchError) as missing:
        load_settings(tmp_path / "other")
    assert missing.value.message.what_happened == f"{tmp_path / 'other'} holds no enrolled runner."


def test_a_grant_for_another_station_is_refused(tmp_path: Path) -> None:
    svc, _ = service(tmp_path)
    issued = svc.issue(STATION, by="admin")

    class Swapped(InProcessPoster):
        def post(self, url: str, body: bytes, *, cafile: str) -> tuple[int, bytes]:
            status, payload = super().post(url, body, cafile=cafile)
            return status, payload.replace(b'"station":"station-07"', b'"station":"station-08"')

    with pytest.raises(BatchError) as exc:
        enrol(
            platform_url="https://x",
            station=STATION,
            code=issued.code,
            runner_url="https://s7",
            state_dir=tmp_path / "state",
            cafile=tmp_path / "ca",
            poster=Swapped(svc),
        )
    assert exc.value.message.what_happened == (
        "The platform answered for station-08, not for station-07."
    )
    assert not (tmp_path / "state").exists()


# --- the real thing: openssl CA, TLS enrolment endpoint, mTLS runner ---------------------------


needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to mint certificates"
)


def test_san_entries_tell_ip_from_dns() -> None:
    assert san_entries(["10.0.0.7", "station-07", "10.0.0.7", "::1"]) == (
        "IP:10.0.0.7,DNS:station-07,IP:::1"
    )
    assert san_entries([]) == ""


@needs_openssl
def test_end_to_end_a_station_enrols_over_tls_and_then_serves_the_executor_over_mtls(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    ca = OpensslCa(
        ca_cert=data_root / "Factory" / "ca" / "ca.pem",
        ca_key=data_root / "Factory" / "ca" / "ca.key",
        runner=LocalProcessRunner(base_path=os.environ.get("PATH", "/usr/bin:/bin")),
        workdir=tmp_path / "work",
    )
    assert ca.ensure_ca() is True and ca.ensure_ca() is False
    assert (data_root / "Factory/ca/ca.key").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "work").glob("*")) if (tmp_path / "work").exists() else True

    registry = StationRegistry(data_root / "Factory" / "stations.json")
    registry.put(StationRecord(name=STATION, allowed_programs=["echo"]))
    svc = EnrolmentService(registry, ca=ca, clock=TickingClock(), data_root=data_root)

    # The executor's own certificates: one to serve enrolment, one to call runners.
    executor_cert = ca.issue_cert("factory-executor", san_hosts=["127.0.0.1"])
    exe_dir = data_root / "Factory" / "ca"
    (exe_dir / "executor.pem").write_text(executor_cert.cert_pem)
    (exe_dir / "executor.key").write_text(executor_cert.key_pem)
    assert not list((tmp_path / "work").glob("*")), "issued keys are shredded on the platform"

    endpoint = EnrolmentServer(
        svc,
        bind="127.0.0.1:0",
        certfile=str(exe_dir / "executor.pem"),
        keyfile=str(exe_dir / "executor.key"),
    )
    endpoint.start()
    bundle_ca = copy_ca_for_bundle(ca.ca_cert, tmp_path / "bundle")
    assert bundle_ca.name == "slas-ca.pem"
    state = tmp_path / "station-state"
    try:
        issued = svc.issue(STATION, by="admin")
        # The station has only the bundle's CA and the code.
        result = enrol(
            platform_url=f"https://127.0.0.1:{endpoint.port}",
            station=STATION,
            code=issued.code,
            runner_url="https://127.0.0.1:0",
            state_dir=state,
            cafile=bundle_ca,
            poster=UrllibPoster(),
            bind="127.0.0.1:0",
        )
        assert result.settings.cert_fingerprint.startswith("SHA256:")
        with pytest.raises(BatchError) as refused:
            enrol(
                platform_url=f"https://127.0.0.1:{endpoint.port}",
                station=STATION,
                code=issued.code,
                runner_url="https://127.0.0.1:0",
                state_dir=tmp_path / "again",
                cafile=bundle_ca,
                poster=UrllibPoster(),
            )
        assert refused.value.message.what_happened == ("No enrolment code is open for station-07.")
        with pytest.raises(BatchError) as unreachable:
            UrllibPoster().post("https://127.0.0.1:9/enrol", b"{}", cafile=str(bundle_ca))
        assert unreachable.value.message.what_happened.endswith("did not answer.")
    finally:
        endpoint.stop()

    # Now the enrolled files serve a runner; the executor reaches it with its certificate and
    # signs with the key the platform kept under Factory/keys.
    runner, settings, how = cli.build_runner(state, screen_backend="fake")
    assert how == "the fake screen (smoke test only)"
    server = RunnerServer(
        runner,
        bind="127.0.0.1:0",
        certfile=str(state / "client.pem"),
        keyfile=str(state / "client.key"),
        cafile=str(state / "ca.pem"),
    )
    server.start()
    try:
        record = registry.get(STATION)
        assert record.batch_key_id == settings.batch_key_id
        key = LocalCredentialResolver(root=data_root).resolve(svc.batch_key_ref(STATION))
        client = MtlsRunnerClient(
            f"https://127.0.0.1:{server.port}",
            certfile=str(exe_dir / "executor.pem"),
            keyfile=str(exe_dir / "executor.key"),
            cafile=str(ca.ca_cert),
        )
        batch = StepBatch(
            batch_id=new_batch_id("T-factory-0001", "hello", 1),
            ticket_id="T-factory-0001",
            station=STATION,
            issued_at=datetime.now(UTC),
            kind="command",
            command=["echo", "hello from the station"],
        )
        answer = client.send(sign_batch(batch, key_id=settings.batch_key_id, key=key.encode()))
        assert answer.ok and answer.command is not None
        assert answer.command.stdout == "hello from the station\n"

        # A caller without a certificate signed by the platform CA never gets past the handshake.
        bare = ssl.create_default_context(cafile=str(ca.ca_cert))
        import urllib.request

        with pytest.raises((ssl.SSLError, OSError)):
            urllib.request.urlopen(
                f"https://127.0.0.1:{server.port}/health", timeout=5, context=bare
            )
    finally:
        server.stop()

    # `doctor` on the enrolled directory is happy with the fake backend.
    out = io.StringIO()
    assert (
        cli.main(["--state-dir", str(state), "doctor", "--screen-backend", "fake"], stdout=out) == 0
    )
    assert "Summary: the station is ready to serve." in out.getvalue()
