"""The xdotool backend builds argv only; the screen worker manages displays through a runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from slas_screen.model import Point, Window
from slas_screen.xdotool import CommandResult, FakeCommandRunner, XdotoolBackend
from slas_screen_worker.session import (
    DisplaySession,
    FakeProcessRunner,
    SessionError,
    SessionManager,
)


def test_xdotool_argv_and_stdin(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    runner.outputs[("xdotool", "search", "--onlyvisible", "--name", ".")] = CommandResult(
        0, "101\n102\n"
    )
    runner.outputs[("xdotool", "getwindowname", "101")] = CommandResult(0, "Login\n")
    runner.outputs[("xdotool", "getwindowclassname", "101")] = CommandResult(0, "burnin\n")
    runner.outputs[("xdotool", "getwindowname", "102")] = CommandResult(0, "BurnIn v3.2\n")
    runner.outputs[
        (
            "xdotool",
            "getactivewindow",
        )
    ] = CommandResult(0, "101\n")
    backend = XdotoolBackend(runner, display=":10")

    windows = backend.windows()
    assert windows == [
        Window(id="101", title="Login", wm_class="burnin"),
        Window(id="102", title="BurnIn v3.2", wm_class=""),
    ]
    assert backend.focused() == Window(id="101", title="Login", wm_class="burnin")

    backend.activate(windows[0])
    backend.click_at(Point(x=40, y=50), button="right", count=2)
    backend.type_text("Sup3rSecret!")
    backend.press("ctrl+s")
    backend.scroll("down", 3)
    backend.screenshot(tmp_path / "shots" / "a.png")

    argv_only = [call[0] for call in runner.calls]
    assert ("xdotool", "windowactivate", "--sync", "101") in argv_only
    assert ("xdotool", "mousemove", "--sync", "40", "50") in argv_only
    assert ("xdotool", "click", "--repeat", "2", "--delay", "80", "3") in argv_only
    assert ("xdotool", "key", "--clearmodifiers", "ctrl+s") in argv_only
    assert ("xdotool", "click", "--repeat", "3", "--delay", "30", "5") in argv_only
    assert (
        "import",
        "-display",
        ":10",
        "-window",
        "root",
        str(tmp_path / "shots" / "a.png"),
    ) in argv_only
    typed = next(call for call in runner.calls if call[0][:2] == ("xdotool", "type"))
    assert typed == (("xdotool", "type", "--delay", "20", "--file", "-"), "Sup3rSecret!")
    assert all("Sup3rSecret!" not in " ".join(call[0]) for call in runner.calls), (
        "secrets never in argv"
    )
    assert (
        backend.find_text("x") is None
        and backend.find_image("x") is None
        and backend.find_target("x") is None
    )
    runner.outputs[("xdotool", "getactivewindow")] = CommandResult(1, "")
    assert backend.focused() is None


def test_display_session_argv() -> None:
    session = DisplaySession(session_id="s-42", display=12)
    assert session.display_name == ":12" and session.vnc_port == 5912 and session.novnc_port == 6092
    assert session.xvfb_argv() == [
        "Xvfb",
        ":12",
        "-screen",
        "0",
        "1920x1080x24",
        "-nolisten",
        "tcp",
        "-noreset",
    ]
    assert "-localhost" in session.x11vnc_argv() and "-rfbport" in session.x11vnc_argv()
    assert session.novnc_argv() == [
        "websockify",
        "--web",
        "/usr/share/novnc",
        "6092",
        "localhost:5912",
    ]
    assert session.sentence() == "Session s-42 has display :12 (1920×1080); watch it on port 6092."
    with pytest.raises(ValueError):
        DisplaySession(session_id="bad id", display=12)


def test_session_manager_allocates_displays_and_enforces_the_limit() -> None:
    runner = FakeProcessRunner()
    manager = SessionManager(runner, max_displays=2)
    assert manager.status_sentence() == "No sessions; 2 displays free."
    first = manager.open("a")
    second = manager.open("b", width=1280, height=720)
    assert (first.display, second.display) == (10, 11)
    assert manager.open("a") is first or manager.open("a").display == 10, (
        "opening again is idempotent"
    )
    assert [h.argv[0] for h in runner.started] == ["Xvfb", "x11vnc", "websockify"] * 2
    assert manager.status_sentence() == "2 of 2 displays in use."
    with pytest.raises(SessionError) as raised:
        manager.open("c")
    assert raised.value.message.what_happened == "All 2 virtual displays are in use."
    assert manager.healthy("a")
    runner.dead.add(runner.started[1].pid)
    assert not manager.healthy("a")
    manager.close("a")
    assert runner.stopped[:3] == [
        runner.started[2].pid,
        runner.started[1].pid,
        runner.started[0].pid,
    ]
    assert not manager.healthy("a")
    manager.close("a")  # closing twice is fine
    third = manager.open("c")
    assert third.display == 10, "the freed display is reused"
