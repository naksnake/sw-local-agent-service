"""The station runner: signed batches (signature, expiry, replay, wrong station), the fake
station driven through the runner (GUI steps with screenshots, secrets typed but never
journalled, allowlisted commands, state for the backup), and real mTLS on loopback."""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_skills.compiler import compile_skill
from slas_skills.library import STATION_LOGIN_BURNIN
from slas_skills.schema import parse_skill
from slas_station_runner.fakes import FakeStation
from slas_station_runner.protocol import (
    BatchError,
    SignedBatch,
    StepBatch,
    new_batch_id,
    sign_batch,
    verify_batch,
)
from slas_station_runner.server import InProcessRunnerClient, MtlsRunnerClient, RunnerServer

KEY = b"k" * 32
KEY_ID = "factory-2026-09"
NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
PASSWORD = "Op3rator-Passw0rd!"


def command_batch(*argv: str, station: str = "station-07", n: int = 1) -> StepBatch:
    return StepBatch(
        batch_id=new_batch_id("T-factory-0001", "cmd", n),
        ticket_id="T-factory-0001",
        station=station,
        issued_at=NOW,
        kind="command",
        command=list(argv),
    )


def skill_batch(n: int = 1) -> tuple[StepBatch, str]:
    skill = parse_skill(STATION_LOGIN_BURNIN)
    compiled = compile_skill(
        skill, {"station": "station-07", "password": "resolved-at-dispatch"}, job_id="j", now=NOW
    )
    handle = compiled.secret_handles["password"]
    return (
        StepBatch(
            batch_id=new_batch_id("T-factory-0001", "login", n),
            ticket_id="T-factory-0001",
            station="station-07",
            issued_at=NOW,
            kind="skill",
            compiled=compiled,
            secrets={handle: PASSWORD},
        ),
        handle,
    )


# --- protocol -----------------------------------------------------------------------------------


def test_signature_expiry_replay_and_station_are_all_checked() -> None:
    batch = command_batch("fixture-ctl", "power", "on")
    signed = sign_batch(batch, key_id=KEY_ID, key=KEY)
    assert signed.algorithm == "hmac-sha256" and len(signed.signature) == 64
    seen: set[str] = set()
    verified = verify_batch(signed, keys={KEY_ID: KEY}, station="station-07", now=NOW, seen=seen)
    assert verified == batch and seen == {batch.batch_id}

    with pytest.raises(BatchError, match="already performed"):
        verify_batch(signed, keys={KEY_ID: KEY}, station="station-07", now=NOW, seen=seen)
    with pytest.raises(BatchError, match="addressed to station-07, not to station-08"):
        verify_batch(signed, keys={KEY_ID: KEY}, station="station-08", now=NOW, seen=set())
    with pytest.raises(BatchError, match="unknown key"):
        verify_batch(signed, keys={"other": KEY}, station="station-07", now=NOW, seen=set())
    with pytest.raises(BatchError, match="does not match its content"):
        verify_batch(signed, keys={KEY_ID: b"x" * 32}, station="station-07", now=NOW, seen=set())
    tampered = signed.model_copy(
        update={"batch": batch.model_copy(update={"command": ["fixture-ctl", "power", "off"]})}
    )
    with pytest.raises(BatchError, match="does not match its content"):
        verify_batch(tampered, keys={KEY_ID: KEY}, station="station-07", now=NOW, seen=set())
    late = NOW + timedelta(seconds=301)
    with pytest.raises(BatchError, match="expired at"):
        verify_batch(signed, keys={KEY_ID: KEY}, station="station-07", now=late, seen=set())
    assert batch.canonical() == batch.model_copy().canonical(), "canonical form is stable"
    assert new_batch_id("T-1", "s", 1) != new_batch_id("T-1", "s", 2)


# --- the runner on the fake station ---------------------------------------------------------------


def test_the_login_skill_runs_on_the_fake_station_and_the_secret_never_lands_in_a_journal(
    tmp_path: Path,
) -> None:
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    station.trust(KEY_ID, KEY)
    batch, _ = skill_batch()
    result = InProcessRunnerClient(station.runner).send(sign_batch(batch, key_id=KEY_ID, key=KEY))

    assert result.ok and result.kind == "skill"
    assert result.skill is not None and result.skill.status == "done"
    assert [r.status for r in result.skill.steps] == ["done"] * 9
    assert result.sentence.endswith("9 of 9 steps finished.")
    # GUI happened on the station: login typed (the secret, not the handle), BurnIn started.
    assert station.login_typed == ["operator", PASSWORD]
    assert station.test_started is True
    assert "BurnIn v3.2" in [w.title for w in station.screen.windows()]
    # Screenshots come back as PNG bytes, two per GUI step (before and after).
    assert len(result.screenshots) >= 16
    assert all(s.png_base64 and s.name.endswith(".png") for s in result.screenshots)
    # The runner's journal masks the secret step and never carries the secret or the handle's value.
    journal = (tmp_path / "station" / "runner-journal.jsonl").read_text(encoding="utf-8")
    assert PASSWORD not in journal
    assert '"text": "[secret]"' in journal
    assert result.skill.outputs["burnin_status"]["stdout"].startswith('{"state": "running"}')
    assert station.shell.argv_seen()[-1] == ["burnin-ctl", "status", "--json"]
    assert station.runner.handled == [batch.batch_id]


def test_commands_are_allowlisted_and_state_is_collected(tmp_path: Path) -> None:
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    station.trust(KEY_ID, KEY)
    client = InProcessRunnerClient(station.runner)

    power = client.send(
        sign_batch(command_batch("fixture-ctl", "power", "on"), key_id=KEY_ID, key=KEY)
    )
    assert power.ok and power.command is not None and power.command.stdout == "fixture power on\n"
    assert station.power == "on"
    assert power.sentence == "fixture-ctl exited 0 on station-07."

    with pytest.raises(BatchError) as info:
        client.send(sign_batch(command_batch("rm", "-rf", "/", n=2), key_id=KEY_ID, key=KEY))
    assert info.value.message.what_happened == "rm is not on station-07's allowlist."
    assert "never a shell" in info.value.message.likely_cause
    assert station.shell.argv_seen() == [["fixture-ctl", "power", "on"]], "nothing else ran"

    state_batch = StepBatch(
        batch_id=new_batch_id("T-factory-0001", "backup", 1),
        ticket_id="T-factory-0001",
        station="station-07",
        issued_at=NOW,
        kind="state",
    )
    state = client.send(sign_batch(state_batch, key_id=KEY_ID, key=KEY))
    assert state.ok and state.state is not None
    assert state.state.versions == {
        "burnin": "3.2.1",
        "station-runner": "0.0.1",
        "os": "station-os 5.4",
    }
    assert [Path(p).name for p in state.state.files] == ["burnin.ini", "station.log"]
    assert state.sentence == "Collected 2 files and 3 versions from station-07."

    # A batch signed with a key the station does not trust is refused before anything runs.
    stranger = sign_batch(command_batch("fixture-ctl", "power", "off", n=3), key_id="nope", key=KEY)
    with pytest.raises(BatchError, match="unknown key"):
        client.send(stranger)
    assert station.power == "on"

    # A skill step that is not a screen step and not an allowlisted run is refused too.
    from slas_schemas.plan import Plan, Step
    from slas_skills.compiler import CompiledSkill

    rogue = CompiledSkill(
        skill_id="rogue",
        plan=Plan(
            id="p",
            job_id="j",
            summary="rogue",
            created_at=NOW,
            steps=[
                Step(
                    id="a",
                    n=1,
                    primitive="run",
                    title="run sh",
                    args={"command": ["sh", "-c", "id"]},
                ),
            ],
        ),
    )
    rogue_batch = StepBatch(
        batch_id=new_batch_id("T-factory-0001", "rogue", 1),
        ticket_id="T-factory-0001",
        station="station-07",
        issued_at=NOW,
        kind="skill",
        compiled=rogue,
    )
    outcome = client.send(sign_batch(rogue_batch, key_id=KEY_ID, key=KEY))
    assert outcome.ok is False
    assert outcome.skill is not None
    assert (
        outcome.skill.steps[0].sentence == "sh is not on this station's allowlist; nothing was run."
    )


def test_a_planted_failure_shows_on_the_screen_and_in_the_result(tmp_path: Path) -> None:
    station = FakeStation(
        "station-07", state_dir=tmp_path / "station", clock=FakeClock(), plant="fail_result"
    )
    station.trust(KEY_ID, KEY)
    client = InProcessRunnerClient(station.runner)
    batch, _ = skill_batch()
    assert client.send(sign_batch(batch, key_id=KEY_ID, key=KEY)).ok
    result = client.send(
        sign_batch(command_batch("burnin-ctl", "result", "--json"), key_id=KEY_ID, key=KEY)
    )
    assert result.command is not None
    assert json.loads(result.command.stdout)["result"] == "FAIL"
    assert "FAIL" in station.screen.visible_texts

    silent = FakeStation(
        "station-08", state_dir=tmp_path / "s8", clock=FakeClock(), plant="no_burnin_window"
    )
    silent.trust(KEY_ID, KEY)
    batch, _ = skill_batch()
    outcome = InProcessRunnerClient(silent.runner).send(
        sign_batch(batch.model_copy(update={"station": "station-08"}), key_id=KEY_ID, key=KEY)
    )
    assert outcome.ok is False and outcome.skill is not None
    assert outcome.skill.status == "failed"
    assert outcome.skill.steps[-1].sentence == "'BurnIn v3.2' did not appear within 60 seconds."
    assert outcome.skill.steps[-1].screenshots[-1].endswith("-failure.png"), "screenshot_and_stop"


# --- mTLS on loopback ------------------------------------------------------------------------


def _openssl(*argv: str, cwd: Path) -> None:
    subprocess.run(["openssl", *argv], cwd=cwd, check=True, capture_output=True, timeout=60)


def make_certs(directory: Path) -> dict[str, str]:
    """A throwaway CA, a runner certificate for 127.0.0.1 and an executor client certificate."""
    directory.mkdir(parents=True, exist_ok=True)
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "2",
        "-subj",
        "/CN=slas-test-ca",
        cwd=directory,
    )
    for name, subject, ext in (
        ("runner", "/CN=station-07", "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth"),
        ("executor", "/CN=factory-executor", "extendedKeyUsage=clientAuth"),
        ("stranger", "/CN=stranger", "extendedKeyUsage=clientAuth"),
    ):
        (directory / f"{name}.ext").write_text(ext + "\n", encoding="utf-8")
        _openssl(
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
            "-subj",
            subject,
            cwd=directory,
        )
        signer = ("ca.pem", "ca.key") if name != "stranger" else ("stranger.csr", "stranger.key")
        if name == "stranger":
            _openssl(
                "x509",
                "-req",
                "-in",
                "stranger.csr",
                "-signkey",
                "stranger.key",
                "-out",
                "stranger.pem",
                "-days",
                "2",
                "-extfile",
                "stranger.ext",
                cwd=directory,
            )
            continue
        _openssl(
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            signer[0],
            "-CAkey",
            signer[1],
            "-CAcreateserial",
            "-out",
            f"{name}.pem",
            "-days",
            "2",
            "-extfile",
            f"{name}.ext",
            cwd=directory,
        )
    return {name: str(directory / name) for name in ("ca", "runner", "executor", "stranger")}


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to mint test certificates"
)
def test_mtls_admits_the_executor_certificate_and_refuses_everyone_else(tmp_path: Path) -> None:
    certs = make_certs(tmp_path / "certs")
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    station.trust(KEY_ID, KEY)
    server = RunnerServer(
        station.runner,
        bind="127.0.0.1:0",
        certfile=f"{certs['runner']}.pem",
        keyfile=f"{certs['runner']}.key",
        cafile=f"{certs['ca']}.pem",
    )
    server.start()
    try:
        url = f"https://127.0.0.1:{server.port}"
        client = MtlsRunnerClient(
            url,
            certfile=f"{certs['executor']}.pem",
            keyfile=f"{certs['executor']}.key",
            cafile=f"{certs['ca']}.pem",
        )
        result = client.send(
            sign_batch(command_batch("fixture-ctl", "power", "on"), key_id=KEY_ID, key=KEY)
        )
        assert result.ok and station.power == "on"
        assert client.sent == [command_batch("fixture-ctl", "power", "on").batch_id]

        # The signature is checked behind the TLS gate too: a bad key is a three-part 403.
        with pytest.raises(BatchError) as info:
            client.send(
                sign_batch(
                    command_batch("fixture-ctl", "power", "off", n=2), key_id="nope", key=KEY
                )
            )
        assert info.value.message.what_happened == "The batch is signed with an unknown key (nope)."
        assert station.power == "on"

        # A replay of the first batch is refused.
        with pytest.raises(BatchError, match="already performed"):
            client.send(
                sign_batch(command_batch("fixture-ctl", "power", "on"), key_id=KEY_ID, key=KEY)
            )

        # No client certificate: the handshake fails, nothing reaches the runner.
        plain = ssl.create_default_context(cafile=f"{certs['ca']}.pem")
        with pytest.raises((urllib.error.URLError, ssl.SSLError, ConnectionError)):
            urllib.request.urlopen(f"{url}/health", context=plain, timeout=5)  # noqa: S310

        # A certificate from another CA: refused at the handshake as well.
        stranger = MtlsRunnerClient(
            url,
            certfile=f"{certs['stranger']}.pem",
            keyfile=f"{certs['stranger']}.key",
            cafile=f"{certs['ca']}.pem",
        )
        with pytest.raises(BatchError) as info:
            stranger.send(
                sign_batch(
                    command_batch("fixture-ctl", "power", "off", n=3), key_id=KEY_ID, key=KEY
                )
            )
        assert info.value.message.what_happened == f"The station runner at {url} did not answer."
        assert station.power == "on"

        # Health with the right certificate.
        health = urllib.request.urlopen(  # noqa: S310
            f"{url}/health", context=client.context, timeout=5
        )
        assert json.loads(health.read()) == {"station": "station-07", "ok": True}
        assert len(station.runner.handled) == 1
    finally:
        server.stop()


def test_signed_batch_round_trips_through_json() -> None:
    batch, _ = skill_batch()
    signed = sign_batch(batch, key_id=KEY_ID, key=KEY)
    again = SignedBatch.model_validate_json(signed.model_dump_json())
    assert again == signed
    assert (
        verify_batch(again, keys={KEY_ID: KEY}, station="station-07", now=NOW, seen=set()) == batch
    )
