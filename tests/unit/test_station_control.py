"""Watch and take over (P10): the operator pauses the runner at a step boundary, drives the
station through the VNC view, and resumes or aborts. The VNC bytes travel over the runner's
mTLS channel; a station without VNC says so in a sentence."""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_schemas.ticket import TicketState
from slas_screen.backend import FakeScreen
from slas_screen.model import Point, ScreenStopError
from slas_screen.policy import ScreenPolicy
from slas_station_runner.control import Controller, PausableScreen
from slas_station_runner.fakes import FakeStation
from slas_station_runner.protocol import (
    BatchError,
    BatchResult,
    StepBatch,
    new_batch_id,
    sign_batch,
)
from slas_station_runner.server import (
    InProcessRunnerClient,
    MtlsRunnerClient,
    RunnerServer,
    VncTunnel,
    client_context,
)
from tests.unit.test_factory_executor import Line
from tests.unit.test_station_runner import KEY, KEY_ID, make_certs, skill_batch

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def control_batch(verb: str, by: str = "lee", n: int = 1) -> StepBatch:
    return StepBatch(
        batch_id=new_batch_id("T-factory-0001", f"control-{verb}", n),
        ticket_id="T-factory-0001",
        station="station-07",
        issued_at=NOW,
        kind="control",
        command=[verb, by],
    )


def wait_until(predicate: object, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return False


# --- the controller ---------------------------------------------------------------------------


def test_pause_blocks_a_gui_step_until_resume_and_abort_stops_it_with_a_sentence() -> None:
    clock = FakeClock()
    controller = Controller("station-07", clock, poll_s=0.01)
    assert controller.status().sentence() == "The runner drives station-07."

    state = controller.pause("lee")
    assert state.paused and state.sentence() == (
        "lee has taken over station-07; the runner sends no input until it is resumed."
    )
    passed = threading.Event()

    def pass_checkpoint() -> None:
        controller.checkpoint()
        passed.set()

    worker = threading.Thread(target=pass_checkpoint, daemon=True)
    worker.start()
    time.sleep(0.05)
    assert not passed.is_set() and controller.waits > 0, "the step waits at the boundary"
    controller.resume("lee")
    assert passed.wait(2)

    controller.abort("lee")
    with pytest.raises(ScreenStopError) as exc:
        controller.checkpoint()
    assert exc.value.message.what_happened == "Stopped: lee took over station-07."
    assert controller.status().sentence() == "lee aborted the run on station-07."
    controller.resume("lee")
    controller.checkpoint()  # a resume hands the station back to the runner


def test_pausable_screen_checks_before_every_primitive(tmp_path: Path) -> None:
    clock = FakeClock()
    controller = Controller("station-07", clock, poll_s=0.01)
    backend = FakeScreen()
    backend.add_window("w1", "BurnIn v3.2", "burnin", focused=True)
    backend.show_text("Start test")
    backend.targets["#go"] = Point(x=5, y=5)
    screen = PausableScreen(
        backend,
        policy=ScreenPolicy(),
        clock=clock,
        screenshots_dir=tmp_path / "screens",
        sleep=lambda _s: None,
        controller=controller,
    )
    assert screen.focus_window(title="BurnIn").ok
    assert screen.click(text="Start test").ok
    assert screen.type_text("abc").ok and screen.key("Enter").ok
    assert screen.scroll("down", 2).ok
    assert screen.wait_for(text="Start test", timeout_s=1).ok
    assert screen.assert_visible(text="Start test", message="BurnIn shows the button").ok
    actions_before = len(backend.actions)

    controller.pause("lee")
    done: list[bool] = []
    worker = threading.Thread(
        target=lambda: done.append(screen.click(target="#go").ok), daemon=True
    )
    worker.start()
    time.sleep(0.05)
    assert done == [] and len(backend.actions) == actions_before, "no input while paused"
    controller.resume("lee")
    worker.join(2)
    assert done == [True]

    controller.abort("lee")
    with pytest.raises(ScreenStopError):
        screen.key("Enter")
    assert len(backend.actions) == actions_before + 1


# --- through the runner and the executor -----------------------------------------------------


def take_over_station(tmp_path: Path) -> tuple[FakeStation, Controller]:
    clock = FakeClock()
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=clock)
    station.trust(KEY_ID, KEY)
    controller = Controller("station-07", clock, poll_s=0.01)
    station.runner.screen = PausableScreen(
        station.screen,
        policy=ScreenPolicy(),
        clock=clock,
        screenshots_dir=tmp_path / "station" / "screens",
        sleep=lambda _s: None,
        controller=controller,
    )
    station.runner.controller = controller
    return station, controller


def test_control_batches_pause_resume_and_abort_a_skill_on_the_station(tmp_path: Path) -> None:
    station, _controller = take_over_station(tmp_path)
    client = InProcessRunnerClient(station.runner)

    def control(verb: str, n: int) -> BatchResult:
        return client.send(sign_batch(control_batch(verb, n=n), key_id=KEY_ID, key=KEY))

    assert control("status", 1).sentence == "The runner drives station-07."
    paused = control("pause", 2)
    assert paused.ok and paused.control is not None and paused.control.paused
    assert paused.sentence == (
        "lee has taken over station-07; the runner sends no input until it is resumed."
    )

    batch, _ = skill_batch(1)
    results: list[BatchResult] = []
    worker = threading.Thread(
        target=lambda: results.append(client.send(sign_batch(batch, key_id=KEY_ID, key=KEY))),
        daemon=True,
    )
    worker.start()
    time.sleep(0.05)
    assert results == [] and station.login_typed == [], "the login waits for the operator"
    assert control("resume", 3).sentence == "The runner drives station-07."
    worker.join(5)
    assert len(results) == 1 and results[0].ok, results
    assert station.test_started, "the skill finished after the operator handed back control"

    assert control("abort", 4).sentence == "lee aborted the run on station-07."
    second, _ = skill_batch(2)
    stopped = client.send(sign_batch(second, key_id=KEY_ID, key=KEY))
    assert not stopped.ok and stopped.skill is not None
    assert stopped.skill.status != "done"
    assert stopped.skill.steps[0].sentence == "Stopped: lee took over station-07."
    assert stopped.sentence.endswith("failed after step 1 of 9.")

    with pytest.raises(BatchError) as exc:
        client.send(sign_batch(control_batch("dance", n=5), key_id=KEY_ID, key=KEY))
    assert exc.value.message.what_happened == "'dance' is not a control verb."

    entries = [
        json.loads(line)
        for line in (tmp_path / "station" / "runner-journal.jsonl").read_text().splitlines()
    ]
    controls = [e for e in entries if e["kind"] == "control"]
    assert [c["payload"]["verb"] for c in controls] == ["status", "pause", "resume", "abort"]
    assert all(c["payload"]["by"] == "lee" for c in controls)


def test_a_station_without_operator_control_says_so(tmp_path: Path) -> None:
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    station.trust(KEY_ID, KEY)
    with pytest.raises(BatchError) as exc:
        station.runner.handle(sign_batch(control_batch("pause"), key_id=KEY_ID, key=KEY))
    assert exc.value.message.what_happened == "station-07 has no operator control enabled."
    assert exc.value.message.what_to_do == (
        "Enable vnc on the station record and re-enrol, then restart the runner."
    )


def test_the_executor_sends_control_batches_for_a_job_and_journals_them(tmp_path: Path) -> None:
    line = Line(tmp_path)
    controller = Controller("station-07", line.clock, poll_s=0.01)
    line.station.runner.controller = controller
    ticket = line.run()
    result = line.executor.control(ticket.id, "pause", by="lee")
    assert result.kind == "control" and result.control is not None and result.control.paused
    assert line.executor.control(ticket.id, "status", by="lee").sentence == (
        "lee has taken over station-07; the runner sends no input until it is resumed."
    )
    assert line.executor.control(ticket.id, "resume", by="lee").sentence == (
        "The runner drives station-07."
    )
    journal = (tmp_path / "Factory" / "Jobs" / ticket.id / "journal.jsonl").read_text()
    entries = [json.loads(line) for line in journal.splitlines()]
    controls = [e for e in entries if "control" in e]
    assert [(c["control"], c["by"]) for c in controls] == [
        ("pause", "lee"),
        ("status", "lee"),
        ("resume", "lee"),
    ]
    with pytest.raises(KeyError):
        line.executor.control("T-factory-9999", "pause", by="lee")


# --- the VNC relay ----------------------------------------------------------------------------


class FakeVncServer:
    """Speaks the first line of RFB and echoes everything after it."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.sock.settimeout(0.2)
        self.port = int(self.sock.getsockname()[1])
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn: socket.socket) -> None:
        with conn:
            conn.sendall(b"RFB 003.008\n")
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(b"echo:" + data)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(1)
        self.sock.close()


def recv_exactly(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is needed to mint test certificates"
)
def test_the_operator_watches_through_the_mtls_relay_and_a_station_without_vnc_says_so(
    tmp_path: Path,
) -> None:
    certs = make_certs(tmp_path / "certs")
    vnc = FakeVncServer()
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    server = RunnerServer(
        station.runner,
        bind="127.0.0.1:0",
        certfile=certs["runner"] + ".pem",
        keyfile=certs["runner"] + ".key",
        cafile=certs["ca"] + ".pem",
        vnc_port=vnc.port,
    )
    server.start()
    tunnel = VncTunnel(
        f"https://127.0.0.1:{server.port}",
        certfile=certs["executor"] + ".pem",
        keyfile=certs["executor"] + ".key",
        cafile=certs["ca"] + ".pem",
    )
    tunnel.start()
    try:
        assert tunnel.sentence() == (
            f"Watch the station at vnc://127.0.0.1:{tunnel.port} (relayed over mTLS to "
            f"https://127.0.0.1:{server.port})."
        )
        viewer = socket.create_connection(("127.0.0.1", tunnel.port), timeout=5)
        with viewer:
            assert recv_exactly(viewer, 12) == b"RFB 003.008\n"
            viewer.sendall(b"key press Enter")
            assert recv_exactly(viewer, 20) == b"echo:key press Enter"
        assert wait_until(lambda: tunnel.connections == 1) and vnc.connections == 1

        # Without a certificate the relay never opens: the handshake refuses the stranger.
        stranger = VncTunnel(
            f"https://127.0.0.1:{server.port}",
            certfile=certs["stranger"] + ".pem",
            keyfile=certs["stranger"] + ".key",
            cafile=certs["ca"] + ".pem",
        )
        stranger.start()
        try:
            refused = socket.create_connection(("127.0.0.1", stranger.port), timeout=5)
            with refused:
                refused.settimeout(5)
                assert refused.recv(16) == b"", "closed without a byte"
        finally:
            stranger.stop()
        assert vnc.connections == 1
    finally:
        tunnel.stop()
        server.stop()
        vnc.stop()

    # A station whose record has VNC off answers /vnc with a sentence, and the tunnel closes.
    quiet = RunnerServer(
        station.runner,
        bind="127.0.0.1:0",
        certfile=certs["runner"] + ".pem",
        keyfile=certs["runner"] + ".key",
        cafile=certs["ca"] + ".pem",
    )
    quiet.start()
    try:
        context = client_context(
            certfile=certs["executor"] + ".pem",
            keyfile=certs["executor"] + ".key",
            cafile=certs["ca"] + ".pem",
        )
        request = urllib.request.Request(
            f"https://127.0.0.1:{quiet.port}/vnc", data=b"", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5, context=context)  # noqa: S310
        assert exc.value.code == 404
        payload = json.loads(exc.value.read())
        assert payload["what_happened"] == "VNC is not enabled on this station."
        assert payload["what_to_do"] == "Enable it under Admin → Stations and re-enrol."

        closed = VncTunnel(
            f"https://127.0.0.1:{quiet.port}",
            certfile=certs["executor"] + ".pem",
            keyfile=certs["executor"] + ".key",
            cafile=certs["ca"] + ".pem",
        )
        closed.start()
        try:
            viewer = socket.create_connection(("127.0.0.1", closed.port), timeout=5)
            with viewer:
                viewer.settimeout(5)
                assert viewer.recv(16) == b""
        finally:
            closed.stop()

        # The mTLS client still works for batches on the same server.
        client = MtlsRunnerClient(
            f"https://127.0.0.1:{quiet.port}",
            certfile=certs["executor"] + ".pem",
            keyfile=certs["executor"] + ".key",
            cafile=certs["ca"] + ".pem",
        )
        station.trust(KEY_ID, KEY)
        with pytest.raises(BatchError) as no_control:
            client.send(sign_batch(control_batch("status"), key_id=KEY_ID, key=KEY))
        assert no_control.value.message.what_happened == (
            "station-07 has no operator control enabled."
        )
    finally:
        quiet.stop()


def test_the_executor_signs_with_the_station_key_from_enrolment_when_one_exists(
    tmp_path: Path,
) -> None:
    from slas_hal.credentials import FakeCredentialResolver
    from tests.unit.test_factory_executor import SECRETS

    line = Line(tmp_path)
    station_key = "s" * 32
    line.station.trust("station-07-20260914100000", station_key.encode())
    line.executor.resolver = FakeCredentialResolver(
        {**SECRETS, "file:Factory/keys/station-07.key": station_key}
    )
    line.executor.station_keys["station-07"] = (
        "file:Factory/keys/station-07.key",
        "station-07-20260914100000",
    )
    ticket = line.run()
    assert ticket.state == TicketState.DONE
    entries = [
        json.loads(line_)
        for line_ in (tmp_path / "station" / "runner-journal.jsonl").read_text().splitlines()
    ]
    key_ids = {e["payload"]["key_id"] for e in entries if e["kind"] == "batch"}
    assert key_ids == {"station-07-20260914100000"}, "every batch used the station's own key"
