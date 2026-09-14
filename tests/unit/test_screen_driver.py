"""The screen driver's rules against the screen fake: screenshots, rate limit, deny-list, stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_screen.backend import TINY_PNG, FakeScreen
from slas_screen.driver import RateLimiter, ScreenDriver
from slas_screen.model import Point, ScreenStopError
from slas_screen.policy import ScreenPolicy

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


class Setup:
    def __init__(self, tmp_path: Path, *, step_ms: int = 0, **policy: object) -> None:
        self.clock = FakeClock(NOW, step=timedelta(milliseconds=step_ms))
        self.fake = FakeScreen()
        self.slept: list[float] = []

        def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            self.clock._now = self.clock._now + timedelta(seconds=seconds)  # the test controls time

        self.driver = ScreenDriver(
            self.fake,
            policy=ScreenPolicy.model_validate(policy),
            clock=self.clock,
            screenshots_dir=tmp_path / "screens",
            sleep=sleep,
        )


def test_every_step_has_a_screenshot_before_and_after(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("w1", "BurnIn v3.2", focused=True)
    s.fake.show_text("Start test")
    focus = s.driver.focus_window(title="burnin")
    assert focus.ok and focus.matched == "BurnIn v3.2"
    click = s.driver.click(text="Start test")
    assert click.ok and click.sentence == "Clicked the text 'Start test'."
    for result in (focus, click):
        assert result.before and result.after
        assert Path(result.before).read_bytes() == TINY_PNG and Path(result.after).is_file()
    assert sorted(p.name for p in (tmp_path / "screens").iterdir()) == [
        "0001-focus-before.png",
        "0002-after.png",
        "0003-click-before.png",
        "0004-after.png",
    ]
    assert s.fake.actions == ["activate w1", "click leftx1 at 100,100"]


def test_type_key_scroll_and_assert(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("w1", "Login", focused=True)
    assert s.driver.type_text("operator").sentence == "Typed 8 characters."
    assert s.driver.key("Tab").sentence == "Pressed Tab."
    assert s.driver.scroll("down", 3).sentence == "Scrolled down by 3."
    assert not s.driver.scroll("sideways", 1).ok
    s.fake.show_text("Ready")
    assert s.driver.assert_visible(text="Ready", message="the app is ready").ok
    missing = s.driver.assert_visible(text="Gone", message="the app is ready")
    assert not missing.ok and missing.sentence == "Not visible: the app is ready."
    assert s.fake.typed == ["operator"]


def test_click_targets_and_coordinate_policy(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("w1", "App", focused=True)
    s.fake.targets["#username"] = Point(x=10, y=20)
    s.fake.images["ok.png"] = Point(x=30, y=40)
    assert s.driver.click(target="#username").sentence == "Clicked the target #username."
    assert s.driver.click(image="ok.png", count=2).sentence == "Double-clicked the image ok.png."
    assert s.driver.click(x=5, y=6, button="right").sentence == "Right-clicked (5, 6)."
    missing = s.driver.click(text="Nowhere")
    assert not missing.ok and missing.sentence == "Could not find the text 'Nowhere' on the screen."
    assert not s.driver.click().ok
    strict = Setup(tmp_path / "strict", allow_coordinates=False)
    strict.fake.add_window("w1", "App", focused=True)
    with pytest.raises(ScreenStopError, match="bare coordinates"):
        strict.driver.click(x=1, y=1)


def test_wait_for_polls_until_the_text_appears_or_times_out(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("w1", "Login", focused=True)
    s.fake.appear_after["BurnIn v3.2"] = 3
    result = s.driver.wait_for(text="BurnIn v3.2", timeout_s=10)
    assert result.ok and result.sentence == "'BurnIn v3.2' appeared."
    assert len(s.slept) == 3
    timeout = s.driver.wait_for(window="Never", timeout_s=2)
    assert not timeout.ok and timeout.sentence == "'Never' did not appear within 2 seconds."
    s.fake.add_window("w2", "BurnIn v3.2")
    window = s.driver.wait_for(window="BurnIn")
    assert window.ok and window.matched == "BurnIn v3.2"


def test_rate_limit_is_ten_actions_per_second(tmp_path: Path) -> None:
    s = Setup(tmp_path, step_ms=0)
    s.fake.add_window("w1", "App", focused=True)
    for _ in range(10):
        s.driver.key("Tab")
    assert s.slept == [], "ten actions in one second are allowed"
    s.driver.key("Tab")
    assert len(s.slept) == 1 and 0 < s.slept[0] <= 1.0, "the eleventh waits for the window to roll"
    assert s.driver.limiter.waits == 1
    limiter = RateLimiter(FakeClock(NOW, step=timedelta(seconds=0)), lambda _s: None, per_second=2)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert limiter.waits == 1


def test_deny_list_stops_hard(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("w1", "App", focused=True)
    s.fake.add_window("w2", "pat@platform-host: ~ — Terminal", "gnome-terminal")
    s.fake.add_window("w3", "KeePassXC")
    with pytest.raises(ScreenStopError) as raised:
        s.driver.focus_window(title="Terminal")
    assert raised.value.message.what_happened == (
        "Stopped: the window 'pat@platform-host: ~ — Terminal' is on the deny-list."
    )
    with pytest.raises(ScreenStopError, match="KeePassXC"):
        s.driver.focus_window(title="KeePass")
    assert "activate w2" not in s.fake.actions and "activate w3" not in s.fake.actions
    assert ScreenPolicy().denied("Bitwarden", "") is not None
    assert ScreenPolicy().denied("BurnIn v3.2", "burnin") is None


def test_unexpected_focus_change_stops_hard(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("w1", "Login", focused=True)
    s.fake.add_window("w2", "Update available")
    assert s.driver.focus_window(title="Login").ok
    s.fake.steal_focus_after = (2, "Update available")
    assert s.driver.key("Tab").ok  # action 2: focus is stolen right after
    with pytest.raises(ScreenStopError) as raised:
        s.driver.type_text("password")
    assert raised.value.message.what_happened == (
        "Stopped: the focused window changed from 'Login' to 'Update available' unexpectedly."
    )
    assert s.fake.typed == [], "nothing was typed into the wrong window"


def test_typing_into_a_denied_focused_window_is_refused(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.fake.add_window("t", "Terminal", "xterm", focused=True)
    with pytest.raises(ScreenStopError, match="deny-list"):
        s.driver.type_text("rm -rf /")
    assert s.fake.typed == []
