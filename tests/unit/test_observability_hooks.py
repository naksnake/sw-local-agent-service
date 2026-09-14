"""The §8.2 metrics are emitted where the work happens: the screen driver, the skill runner,
the kernel, the SOP renderer, the factory executor, the sandbox and screen-worker managers."""

from __future__ import annotations

from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.store import FileTicketStore
from slas_observability import metrics
from slas_observability.metrics import REGISTRY
from slas_sandbox_manager.manager import SandboxManager
from slas_sandbox_manager.runtime import FakeSandboxRuntime
from slas_schemas.job import Upload
from slas_screen.backend import FakeScreen
from slas_screen.driver import ScreenDriver
from slas_screen.model import ScreenStopError
from slas_screen.policy import ScreenPolicy
from slas_screen_worker.session import FakeProcessRunner, SessionManager
from slas_station_runner.fakes import FakeStation
from slas_station_runner.protocol import sign_batch
from tests.unit.test_factory_executor import Line
from tests.unit.test_station_runner import KEY, KEY_ID, skill_batch


def value(name: str, **labels: object) -> float:
    return REGISTRY.value(name, **labels)


def test_the_screen_driver_counts_every_primitive_by_outcome(tmp_path: Path) -> None:
    REGISTRY.reset()
    backend = FakeScreen()
    backend.add_window("w1", "BurnIn v3.2", "burnin", focused=True)
    backend.add_window("w2", "Terminal", "xterm")
    backend.show_text("Start test")
    driver = ScreenDriver(
        backend,
        policy=ScreenPolicy(),
        clock=FakeClock(),
        screenshots_dir=tmp_path / "screens",
        sleep=lambda _s: None,
    )
    assert driver.focus_window(title="BurnIn").ok
    assert not driver.focus_window(title="Nowhere").ok
    assert driver.click(text="Start test").ok
    assert not driver.click(text="Missing").ok
    assert driver.type_text("abc").ok and driver.key("Enter").ok
    assert driver.screenshot("proof").ok
    assert driver.assert_visible(text="Start test", message="visible").ok
    with pytest.raises(ScreenStopError):
        driver.focus_window(title="Terminal")  # the deny-list stops it
    assert value("slas_screen_steps_total", primitive="focus_window", outcome="ok") == 1
    assert value("slas_screen_steps_total", primitive="focus_window", outcome="failed") == 1
    assert value("slas_screen_steps_total", primitive="focus_window", outcome="stopped") == 1
    assert value("slas_screen_steps_total", primitive="click", outcome="ok") == 1
    assert value("slas_screen_steps_total", primitive="click", outcome="failed") == 1
    assert value("slas_screen_steps_total", primitive="type", outcome="ok") == 1
    assert value("slas_screen_steps_total", primitive="key", outcome="ok") == 1
    assert value("slas_screen_steps_total", primitive="screenshot", outcome="ok") == 1
    assert value("slas_screen_steps_total", primitive="assert_visible", outcome="ok") == 1


def test_skill_runs_are_counted_by_skill_and_outcome(tmp_path: Path) -> None:
    REGISTRY.reset()
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    station.trust(KEY_ID, KEY)
    batch, _ = skill_batch(1)
    assert station.runner.handle(sign_batch(batch, key_id=KEY_ID, key=KEY)).ok
    assert value("slas_skill_runs_total", skill="station-login-burnin", outcome="done") == 1
    failing = FakeStation(
        "station-08", state_dir=tmp_path / "s8", clock=FakeClock(), plant="no_burnin_window"
    )
    failing.trust(KEY_ID, KEY)
    batch, _ = skill_batch(2)
    batch = batch.model_copy(update={"station": "station-08"})
    assert not failing.runner.handle(sign_batch(batch, key_id=KEY_ID, key=KEY)).ok
    assert value("slas_skill_runs_total", skill="station-login-burnin", outcome="failed") == 1
    assert value("slas_screen_steps_total", primitive="wait_for", outcome="failed") == 1


def test_the_kernel_counts_turns_transitions_tickets_in_state_and_sop_exports(
    tmp_path: Path,
) -> None:
    REGISTRY.reset()
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=FakeExecutor(),
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
    )
    ticket = kernel.run(Upload(filename="plan.md", uploaded_by="pat", content="#", size_bytes=1))
    steps = len(ticket.plan.steps) if ticket.plan else 0
    assert steps > 0
    assert value("slas_agent_turns_total", agent="null", outcome="done") == steps
    assert REGISTRY.count("slas_step_seconds", agent="null") == steps
    for state in ("Planned", "Approved", "Running", "Analysing", "Done"):
        assert value("slas_ticket_state_changes_total", agent="null", to=state) == 1, state
    assert value("slas_tickets_in_state", agent="null", state="Done") == 1
    assert value("slas_tickets_in_state", agent="null", state="Running") == 0
    assert value("slas_tickets_in_state", agent="null", state="Open") == 0
    assert value("slas_sop_exports_total", lang="en") == 1
    assert value("slas_sop_exports_total", lang="zh-Hant") == 1
    text = metrics.render()
    assert 'slas_tickets_in_state{agent="null",state="Done"} 1\n' in text


def test_the_factory_executor_counts_verdicts_held_stations_and_batches(tmp_path: Path) -> None:
    REGISTRY.reset()
    passing = Line(tmp_path / "pass")
    passing.run()
    assert value("slas_factory_verdicts_total", verdict="PASS", decided_by="voters") == 1
    assert value("slas_station_batches_total", station="station-07", kind="skill", ok="true") >= 1
    assert value("slas_station_batches_total", station="station-07", kind="command", ok="true") >= 3
    assert value("slas_factory_stations_held") == 0

    failing = Line(tmp_path / "fail", plant="fail_result")
    ticket = failing.run()
    assert value("slas_factory_verdicts_total", verdict="FAIL", decided_by="gate") == 1
    assert value("slas_factory_stations_held") == 1
    failing.executor.decide(ticket.id, verdict="PASS", by="lee", note="retested by hand")
    assert value("slas_factory_stations_held") == 0
    assert value("slas_factory_verdicts_total", verdict="PASS", decided_by="line_lead") == 1


def test_sandbox_and_display_gauges_follow_open_and_close(tmp_path: Path) -> None:
    REGISTRY.reset()
    manager = SandboxManager(
        runtime=FakeSandboxRuntime(), data_root=tmp_path, clock=FakeClock(), runsc_available=True
    )
    session = manager.open(
        "pat",
        "bmc",
        image="registry.internal/slas/sandbox-python:3.12.6",
        language="python",
        display_name="Pat",
    )
    assert value("slas_sandbox_sessions_open") == 1
    manager.close(session.id)
    assert value("slas_sandbox_sessions_open") == 0

    displays = SessionManager(FakeProcessRunner())
    displays.open("s1")
    displays.open("s2")
    assert value("slas_screen_displays_open") == 2
    displays.close("s1")
    assert value("slas_screen_displays_open") == 1
