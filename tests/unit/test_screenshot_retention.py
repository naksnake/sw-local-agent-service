"""Screenshot retention and the per-station screen tuning (P10)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from slas_factory_executor.settings import (
    DEFAULT_FACTORY_SETTINGS,
    FactorySettingsError,
    default_settings,
    prune_job_screenshots,
    render_factory_yaml,
    settings_from_mapping,
)
from slas_kernel.clock import FakeClock
from slas_screen.backend import FakeScreen
from slas_screen.driver import ScreenDriver
from slas_screen.policy import ScreenPolicy
from slas_screen.retention import RetentionPolicy, prune_screenshots
from slas_station_runner.fakes import FakeStation
from slas_station_runner.protocol import sign_batch
from slas_station_runner.runner import ScreenTuning
from tests.unit.test_station_runner import KEY, KEY_ID, skill_batch

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def shot(directory: Path, name: str, *, days_old: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x89PNG")
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_old_screenshots_go_but_the_first_last_and_failure_shots_stay(tmp_path: Path) -> None:
    job = tmp_path / "Factory" / "Jobs" / "T-factory-0001" / "screens"
    first = shot(job, "001-focus-before.png", days_old=40)
    shot(job, "002-focus-after.png", days_old=39)
    failure = shot(job, "003-click-failure.png", days_old=38)
    shot(job, "004-type-after.png", days_old=31)
    fresh = shot(job, "005-key-after.png", days_old=5)
    last = shot(job, "006-wait-after.png", days_old=1)
    (job / "notes.txt").write_text("not a screenshot")

    report = prune_screenshots(tmp_path / "Factory" / "Jobs", RetentionPolicy(), now=NOW)
    assert report.sentence() == "Looked at 6 screenshots in 1 job: deleted 2, kept 4."
    assert sorted(p.name for p in job.glob("*.png")) == sorted(
        p.name for p in (first, failure, fresh, last)
    )
    assert (job / "notes.txt").exists()


def test_failed_jobs_keep_theirs_longer_and_max_per_job_trims_the_oldest(tmp_path: Path) -> None:
    jobs = tmp_path / "Factory" / "Jobs"
    for i in range(15):
        shot(jobs / "T-factory-0002" / "screens", f"{i:03d}.png", days_old=100 - i)
    for i in range(12):
        shot(jobs / "T-factory-0003" / "screens", f"{i:03d}.png", days_old=2)
    policy = RetentionPolicy(keep_days=30, keep_failed_days=180, max_per_job=10)
    assert policy.sentence() == (
        "Screenshots are kept 30 days (180 days for failed or held jobs), at most 10 per job."
    )
    report = prune_job_screenshots(tmp_path, policy, now=NOW, held_or_failed=["T-factory-0002"])
    assert report.jobs == 2 and report.scanned == 27
    held = sorted(p.name for p in (jobs / "T-factory-0002" / "screens").glob("*.png"))
    assert len(held) == 10 and held[0] == "000.png" and held[-1] == "014.png"
    assert "006.png" in held and "005.png" not in held, "the oldest go first, first/last stay"
    fresh = sorted(p.name for p in (jobs / "T-factory-0003" / "screens").glob("*.png"))
    assert len(fresh) == 10 and "000.png" in fresh and "011.png" in fresh
    assert report.deleted == 7 and report.kept == 20
    assert prune_screenshots(tmp_path / "nowhere", policy, now=NOW).sentence() == (
        "Looked at 0 screenshots in 0 jobs: deleted 0, kept 0."
    )


def test_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        RetentionPolicy(keep_days=0)
    with pytest.raises(ValidationError):
        RetentionPolicy(max_per_job=5)
    with pytest.raises(FactorySettingsError) as exc:
        settings_from_mapping({"vnc_port": 0}, source="config/factory.yaml")
    assert exc.value.message.what_happened == (
        "The factory settings in config/factory.yaml could not be used."
    )
    assert default_settings().sentences()[1] == (
        "An enrolment code works once, within 15 minutes; 5 wrong codes lock the station until "
        "a new code is issued."
    )
    assert "vnc_port: 5900" in render_factory_yaml(DEFAULT_FACTORY_SETTINGS)


def test_the_runner_prunes_its_own_copies_after_every_skill_batch(tmp_path: Path) -> None:
    station = FakeStation("station-07", state_dir=tmp_path / "station", clock=FakeClock())
    station.trust(KEY_ID, KEY)
    station.runner.config = station.runner.config.model_copy(
        update={"retention": RetentionPolicy(max_per_job=10)}
    )
    batch, _ = skill_batch(1)
    result = station.runner.handle(sign_batch(batch, key_id=KEY_ID, key=KEY))
    assert result.ok and len(result.screenshots) > 10, "the result carries every shot"
    assert station.runner.last_prune is not None
    assert station.runner.last_prune.kept == 10
    assert station.runner.last_prune.deleted == len(result.screenshots) - 10
    assert len(list((tmp_path / "station" / "screens").rglob("*.png"))) == 10


# --- window matching and timing knobs ---------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "wanted", "title", "expected"),
    [
        ("contains", "BurnIn", "BurnIn v3.2 — station-07", True),
        ("contains", "burnin v3", "BurnIn v3.2", True),
        ("exact", "BurnIn v3.2", "BurnIn v3.2", True),
        ("exact", "BurnIn", "BurnIn v3.2", False),
        ("prefix", "BurnIn v3", "BurnIn v3.2 — station-07", True),
        ("prefix", "v3.2", "BurnIn v3.2", False),
        ("regex", r"^BurnIn v3\.\d$", "BurnIn v3.2", True),
        ("regex", r"^BurnIn v4", "BurnIn v3.2", False),
        ("regex", r"(", "BurnIn v3.2", False),
    ],
)
def test_window_matching_modes(mode: str, wanted: str, title: str, expected: bool) -> None:
    policy = ScreenPolicy.model_validate({"window_match": mode})
    assert policy.title_matches(wanted, title) is expected


def test_tuning_applies_settle_and_timeout_scale_to_the_driver(tmp_path: Path) -> None:
    tuning = ScreenTuning(window_match="exact", action_settle_s=0.3, wait_timeout_scale=2.5)
    assert tuning.sentence() == (
        "Windows matched by exact; 0.3 s settle after each action; wait timeouts ×2.5; at most "
        "10 actions per second."
    )
    slept: list[float] = []
    backend = FakeScreen()
    backend.add_window("w1", "BurnIn v3.2", "burnin", focused=True)
    clock = FakeClock(step=timedelta(seconds=0))
    driver = ScreenDriver(
        backend,
        policy=tuning.policy(),
        clock=clock,
        screenshots_dir=tmp_path / "screens",
        sleep=slept.append,
    )
    assert not driver.focus_window(title="BurnIn").ok, "exact mode wants the full title"
    assert driver.focus_window(title="BurnIn v3.2").ok
    assert 0.3 in slept, "the settle sleep happened after the action"

    ticking = FakeClock(step=timedelta(seconds=1))
    driver = ScreenDriver(
        backend,
        policy=tuning.policy(),
        clock=ticking,
        screenshots_dir=tmp_path / "screens2",
        sleep=lambda _s: None,
    )
    result = driver.wait_for(text="never", timeout_s=2)
    assert not result.ok and "within 5 seconds" in result.sentence
    with pytest.raises(ValidationError):
        ScreenTuning(wait_timeout_scale=0)
