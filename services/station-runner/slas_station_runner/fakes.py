"""A fake test station: a scripted screen (Login → BurnIn), a scripted shell and a runner.

The station behaves like the §6.3 example: a Login window, a BurnIn window that appears
after Enter, a "Start test" button, then "Test complete" and a result the vendor tool reports
as JSON. `plant` makes the unit fail in one chosen way so the verdict path can be proven.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from slas_hal.drivers.process import FakeProcessRunner
from slas_hal.hal import CommandResult
from slas_screen.backend import FakeScreen
from slas_screen.driver import ScreenDriver
from slas_screen.model import Point
from slas_screen.policy import ScreenPolicy
from slas_station_runner.runner import StationConfig, StationRunner

Plant = Literal["fail_result", "event_log", "sensor_hot", "no_burnin_window"]
STATION_PROGRAMS = ("fixture-ctl", "burnin-ctl", "sensors-ctl", "evlog", "station-ctl")


class Clock(Protocol):
    def now(self) -> datetime: ...


class FakeStation:
    """The station as the runner sees it; `screen` and `shell` are the scripted backends."""

    def __init__(
        self,
        name: str,
        *,
        state_dir: Path,
        clock: Clock,
        plant: Plant | None = None,
        operator_password: str = "Op3rator-Passw0rd!",  # noqa: S107 — a fake, checked by the fake login
    ) -> None:
        self.name = name
        self.plant = plant
        self.clock = clock
        self.operator_password = operator_password
        self.screen = FakeScreen()
        self.screen.add_window("w-login", "Login", "burnin-login", focused=True)
        self.screen.targets["#username"] = Point(x=40, y=40)
        self.shell = FakeProcessRunner()
        self.power = "off"
        self.test_started = False
        self.login_typed: list[str] = []
        self.config_dir = state_dir / "station-config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.config_dir / "burnin.ini").write_text(
            "[burnin]\nprofile=final-test\n", encoding="utf-8"
        )
        (self.config_dir / "station.log").write_text("boot ok\n", encoding="utf-8")
        self._script_shell()
        self._script_screen()
        self.runner = StationRunner(
            StationConfig(
                station=name,
                allowed_programs=list(STATION_PROGRAMS),
                state_files=[
                    str(self.config_dir / "burnin.ini"),
                    str(self.config_dir / "station.log"),
                ],
                versions_command=["station-ctl", "versions", "--json"],
            ),
            keys={},
            screen=ScreenDriver(
                self.screen,
                policy=ScreenPolicy(),
                clock=clock,
                screenshots_dir=state_dir / "screens",
                sleep=lambda _s: None,
            ),
            processes=self.shell,
            clock=clock,
            state_dir=state_dir,
        )

    def trust(self, key_id: str, key: bytes) -> None:
        self.runner.keys[key_id] = key

    # --- scripting ---------------------------------------------------------------------------

    def _script_screen(self) -> None:
        original_press = self.screen.press
        original_type = self.screen.type_text

        def type_text(text: str) -> None:
            original_type(text)
            self.login_typed.append(text)

        def press(keys: str) -> None:
            original_press(keys)
            logged_in = self.operator_password in self.login_typed
            if keys == "Enter" and self.plant != "no_burnin_window" and logged_in:
                self.screen.add_window("w-burnin", "BurnIn v3.2", "burnin")
                self.screen.show_text("Start test")

        self.screen.press = press  # type: ignore[method-assign]
        self.screen.type_text = type_text  # type: ignore[method-assign]

        original_click = self.screen.click_at

        def click_at(point: Point, *, button: str, count: int) -> None:
            original_click(point, button=button, count=count)
            if "Start test" in self.screen.visible_texts and point == Point(x=100, y=100):
                self.test_started = True
                self.screen.appear_after["Test complete"] = 2
                self.screen.show_text("PASS" if self.plant != "fail_result" else "FAIL")

        self.screen.click_at = click_at  # type: ignore[method-assign]

    def _script_shell(self) -> None:
        station = self

        def power(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
            station.power = argv[-1]
            return CommandResult(exit_code=0, stdout=f"fixture power {argv[-1]}\n")

        def result(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
            if not station.test_started:
                return CommandResult(exit_code=2, stderr="burnin-ctl: no test has run\n")
            failed = ["gpu-memory"] if station.plant == "fail_result" else []
            payload = {
                "unit": "under-test",
                "result": "FAIL" if failed else "PASS",
                "tests": {
                    "cpu": "PASS",
                    "memory": "PASS",
                    "gpu-memory": "FAIL" if failed else "PASS",
                },
                "duration_s": 1860,
            }
            return CommandResult(exit_code=0, stdout=json.dumps(payload) + "\n")

        def sensors(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
            hot = station.plant == "sensor_hot"
            payload = {"cpu_temp_c": 58, "gpu_temp_c": 96 if hot else 61, "psu_ok": True}
            return CommandResult(exit_code=0, stdout=json.dumps(payload) + "\n")

        def evlog(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
            if station.plant == "event_log":
                return CommandResult(
                    exit_code=0,
                    stdout="2026-09-14 08:12:03 Critical: PCIe uncorrectable error slot 3\n",
                )
            return CommandResult(exit_code=0, stdout="")

        def versions(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
            return CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {"burnin": "3.2.1", "station-runner": "0.0.1", "os": "station-os 5.4"}
                )
                + "\n",
            )

        self.shell.on("fixture-ctl", "power", handler=power)
        self.shell.on(
            "burnin-ctl",
            "status",
            result=CommandResult(exit_code=0, stdout='{"state": "running"}\n'),
        )
        self.shell.on("burnin-ctl", "result", handler=result)
        self.shell.on("sensors-ctl", "read", handler=sensors)
        self.shell.on("evlog", "dump", handler=evlog)
        self.shell.on("station-ctl", "versions", handler=versions)
