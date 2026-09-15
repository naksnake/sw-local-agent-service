"""`slas-station-runner`: the command line installed on a test station (P10).

    enrol    redeem the one-time code from Admin → Stations; writes the identity and config
    serve    run the mTLS server (and the VNC server when enabled) with the enrolled identity
    doctor   check the station: identity files, GUI backend, binaries, display, VNC
    windows  list the windows on the station's display — for tuning window matching
    prune    apply the screenshot retention to the runner's own copies now
    show     print the enrolled settings and the station configuration (no secrets)

Linux drives the display through xdotool on the operator's X session; Windows needs a GUI
backend (PyAutoGUI) that is not an approved dependency yet, so `serve` there performs
commands, state and control batches and refuses GUI steps with a sentence.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import ssl
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from slas_hal.drivers.process import LocalProcessRunner, LocalStreamRunner
from slas_schemas.errors import ThreePartMessage
from slas_screen.backend import FakeScreen, ScreenBackend
from slas_screen.model import Point, Window
from slas_screen.xdotool import CommandResult as XdoResult
from slas_screen.xdotool import XdotoolBackend
from slas_station_runner.control import Controller, PausableScreen
from slas_station_runner.enrol import (
    RunnerSettings,
    UrllibPoster,
    enrol,
    load_batch_key,
    load_config,
    load_settings,
)
from slas_station_runner.protocol import BatchError
from slas_station_runner.runner import StationConfig, StationRunner
from slas_station_runner.server import RunnerServer

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SubprocessCommandRunner:
    """xdotool and ImageMagick's `import` as argv on the station, no shell."""

    def run(self, argv: Sequence[str], *, stdin_text: str | None = None) -> XdoResult:
        try:
            completed = subprocess.run(  # noqa: S603 — argv list, no shell
                list(argv),
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError:
            return XdoResult(127, "")
        return XdoResult(completed.returncode, completed.stdout)


class NoScreenBackend:
    """A station without a GUI backend: every GUI step fails with a sentence, nothing else."""

    def windows(self) -> list[Window]:
        return []

    def focused(self) -> Window | None:
        return None

    def activate(self, window: Window) -> None:
        raise RuntimeError(_NO_GUI)

    def find_text(self, text: str) -> Point | None:
        return None

    def find_image(self, image: str) -> Point | None:
        return None

    def find_target(self, target: str) -> Point | None:
        return None

    def click_at(self, point: Point, *, button: str, count: int) -> None:
        raise RuntimeError(_NO_GUI)

    def type_text(self, text: str) -> None:
        raise RuntimeError(_NO_GUI)

    def press(self, keys: str) -> None:
        raise RuntimeError(_NO_GUI)

    def scroll(self, direction: str, amount: int) -> None:
        raise RuntimeError(_NO_GUI)

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


_NO_GUI = (
    "This station has no GUI backend: on Windows the runner needs PyAutoGUI, which is not an "
    "approved dependency yet; on Linux it needs xdotool and a DISPLAY."
)


def choose_backend(settings: RunnerSettings) -> tuple[ScreenBackend, str]:
    wanted = settings.screen_backend
    if wanted == "fake":
        return FakeScreen(), "the fake screen (smoke test only)"
    system = platform.system().lower()
    if wanted in ("auto", "xdotool") and system == "linux":
        display = os.environ.get("DISPLAY") or settings.display
        if shutil.which("xdotool") and display:
            return XdotoolBackend(
                SubprocessCommandRunner(), display=display
            ), f"xdotool on {display}"
    return NoScreenBackend(), "no GUI backend on this station"


def build_runner(
    state_dir: Path, *, screen_backend: str | None = None
) -> tuple[StationRunner, RunnerSettings, str]:
    settings = load_settings(state_dir)
    if screen_backend is not None:
        settings = settings.model_copy(update={"screen_backend": screen_backend})
    config: StationConfig = load_config(state_dir)
    clock = SystemClock()
    backend, how = choose_backend(settings)
    controller = Controller(config.station, clock)
    screen = PausableScreen(
        backend,
        policy=config.screen.policy(),
        clock=clock,
        screenshots_dir=state_dir / "screens",
        sleep=time.sleep,
        controller=controller,
    )
    runner = StationRunner(
        config,
        keys={settings.batch_key_id: load_batch_key(state_dir)},
        screen=screen,
        processes=LocalProcessRunner(base_path=os.environ.get("PATH", "/usr/bin:/bin")),
        clock=clock,
        state_dir=state_dir,
    )
    runner.controller = controller
    return runner, settings, how


# --- commands ---------------------------------------------------------------------------


def run_enrol(args: argparse.Namespace, out: TextIO) -> int:
    try:
        result = enrol(
            platform_url=args.platform,
            station=args.station,
            code=args.code,
            runner_url=args.runner_url,
            state_dir=Path(args.state_dir),
            cafile=Path(args.ca),
            poster=UrllibPoster(),
            bind=args.bind,
        )
    except BatchError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    out.write(result.sentence + "\n")
    out.write("Next: `slas-station-runner doctor`, then `slas-station-runner serve`.\n")
    return EXIT_OK


def run_serve(args: argparse.Namespace, out: TextIO, *, once: bool = False) -> int:
    state_dir = Path(args.state_dir)
    try:
        runner, settings, how = build_runner(state_dir, screen_backend=args.screen_backend)
    except BatchError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    vnc_port = runner.config.vnc.port if runner.config.vnc.enabled else None
    if runner.config.vnc.enabled and runner.config.vnc.command and not once:
        LocalStreamRunner(base_path=os.environ.get("PATH", "/usr/bin:/bin")).start(
            runner.config.vnc.command, env={"DISPLAY": settings.display}
        )
    try:
        server = RunnerServer(
            runner,
            bind=settings.bind,
            certfile=str(state_dir / "client.pem"),
            keyfile=str(state_dir / "client.key"),
            cafile=str(state_dir / "ca.pem"),
            vnc_port=vnc_port,
        )
    except (OSError, ssl.SSLError, ValueError) as exc:
        out.write(
            ThreePartMessage(
                f"The runner could not start its TLS server on {settings.bind}.",
                str(exc),
                "Check that the port is free and that client.pem, client.key and ca.pem under "
                f"{state_dir} are the files enrolment wrote; re-enrol if they are damaged.",
            ).render()
            + "\n"
        )
        return EXIT_PROBLEMS
    out.write(
        f"{settings.station} is serving on {settings.bind} with {how}; VNC "
        f"{'relayed from port ' + str(vnc_port) if vnc_port else 'off'}. "
        "Batches must be signed with the enrolled key.\n"
    )
    if once:
        server.start()
        server.stop()
        return EXIT_OK
    server.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return EXIT_OK


def run_doctor(args: argparse.Namespace, out: TextIO) -> int:
    state_dir = Path(args.state_dir)
    problems = 0
    lines: list[str] = []

    def check(ok: bool, good: str, bad: str) -> None:
        nonlocal problems
        lines.append(("ok   " if ok else "FAIL ") + (good if ok else bad))
        if not ok:
            problems += 1

    check(
        sys.version_info >= (3, 12),
        f"Python {platform.python_version()}",
        f"Python {platform.python_version()} is too old; 3.12 is needed.",
    )
    for name in ("runner.json", "config.json", "ca.pem", "client.pem", "client.key", "batch.key"):
        path = state_dir / name
        check(
            path.is_file(),
            f"{name} present",
            f"{name} is missing under {state_dir}; run `slas-station-runner enrol`.",
        )
        if path.is_file() and name.endswith((".key", ".pem")):
            mode = path.stat().st_mode & 0o777
            check(
                mode == 0o600 or os.name == "nt",
                f"{name} is mode {mode:o}",
                f"{name} is mode {mode:o}; it must be 0600.",
            )
    system = platform.system().lower()
    wanted = getattr(args, "screen_backend", None) or "auto"
    if (state_dir / "runner.json").is_file():
        settings = load_settings(state_dir)
        if getattr(args, "screen_backend", None):
            settings = settings.model_copy(update={"screen_backend": args.screen_backend})
        wanted = settings.screen_backend
        _, how = choose_backend(settings)
        check(not how.startswith("no GUI"), f"GUI backend: {how}", _NO_GUI)
        lines.append("     " + settings.sentence())
    if system == "linux" and wanted in ("auto", "xdotool"):
        check(
            bool(shutil.which("xdotool")),
            "xdotool found",
            "xdotool is not installed; GUI steps need it.",
        )
        check(
            bool(shutil.which("import")),
            "ImageMagick import found (screenshots)",
            "ImageMagick `import` is not installed; screenshots need it.",
        )
        check(
            bool(os.environ.get("DISPLAY")),
            f"DISPLAY is {os.environ.get('DISPLAY')}",
            "DISPLAY is not set; run the runner inside the operator's graphical session.",
        )
    if (state_dir / "config.json").is_file():
        config = load_config(state_dir)
        lines.append("     " + config.screen.sentence())
        lines.append("     " + config.retention.sentence())
        if config.vnc.enabled and wanted not in ("auto", "xdotool"):
            lines.append(
                f"     VNC on port {config.vnc.port} is relayed to the operator; the VNC "
                "server is not checked with this backend."
            )
        elif config.vnc.enabled:
            vnc_binary = config.vnc.command[0] if config.vnc.command else None
            check(
                vnc_binary is None or bool(shutil.which(vnc_binary)),
                f"VNC server {vnc_binary or 'external'} on port {config.vnc.port}",
                f"{vnc_binary} is not installed; the operator cannot watch this station.",
            )
        else:
            lines.append(
                "     VNC is off for this station; the operator cannot watch or take over."
            )
    out.write("\n".join(lines) + "\n")
    noun = "problem" if problems == 1 else "problems"
    out.write(
        "Summary: the station is ready to serve.\n"
        if problems == 0
        else f"Summary: {problems} {noun} must be fixed before serving.\n"
    )
    return EXIT_OK if problems == 0 else EXIT_PROBLEMS


def run_windows(args: argparse.Namespace, out: TextIO) -> int:
    try:
        runner, _settings, how = build_runner(
            Path(args.state_dir), screen_backend=args.screen_backend
        )
    except BatchError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    windows = runner.screen.backend.windows()
    focused = runner.screen.backend.focused()
    if not windows:
        out.write(f"No window is visible ({how}).\n")
        return EXIT_OK
    out.write(f"{len(windows)} {'window' if len(windows) == 1 else 'windows'} ({how}):\n")
    for window in windows:
        marker = " (focused)" if focused is not None and focused.id == window.id else ""
        out.write(f"  {window.title!r} class {window.wm_class!r}{marker}\n")
    out.write(f"Matching mode on this station: {runner.config.screen.window_match}.\n")
    return EXIT_OK


def run_prune(args: argparse.Namespace, out: TextIO) -> int:
    try:
        runner, _settings, _how = build_runner(Path(args.state_dir), screen_backend="fake")
    except BatchError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    out.write(runner.prune().sentence() + "\n")
    return EXIT_OK


def run_show(args: argparse.Namespace, out: TextIO) -> int:
    try:
        settings = load_settings(Path(args.state_dir))
        config = load_config(Path(args.state_dir))
    except BatchError as exc:
        out.write(exc.message.render() + "\n")
        return EXIT_PROBLEMS
    out.write(settings.sentence() + "\n")
    out.write(f"Allowed programs: {', '.join(config.allowed_programs) or 'none'}.\n")
    out.write(config.screen.sentence() + "\n")
    out.write(config.retention.sentence() + "\n")
    out.write(f"VNC: {'on, port ' + str(config.vnc.port) if config.vnc.enabled else 'off'}.\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slas-station-runner",
        description="The SW Local Agent Service runner on a test station.",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("SLAS_RUNNER_STATE") or str(Path.home() / ".slas-station-runner"),
        help="Where the identity and configuration live (default: $SLAS_RUNNER_STATE or "
        "~/.slas-station-runner).",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    en = commands.add_parser("enrol", help="Redeem a one-time code from Admin → Stations.")
    en.add_argument("--platform", required=True, help="https://<factory executor>:<port>")
    en.add_argument("--station", required=True, help="The station name the administrator used")
    en.add_argument("--code", required=True, help="The one-time code, XXXX-XXXX-XXXX")
    en.add_argument(
        "--runner-url", required=True, help="https://<this station>:<port> the executor will use"
    )
    en.add_argument("--ca", required=True, help="slas-ca.pem from the station bundle")
    en.add_argument("--bind", default="0.0.0.0:8443", help="Where to listen (default 0.0.0.0:8443)")
    serve = commands.add_parser("serve", help="Serve batches over mTLS with the enrolled identity.")
    serve.add_argument(
        "--screen-backend", choices=("auto", "xdotool", "fake", "none"), default=None
    )
    serve.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    doc = commands.add_parser("doctor", help="Check the station before serving.")
    doc.add_argument("--screen-backend", choices=("auto", "xdotool", "fake", "none"), default=None)
    win = commands.add_parser("windows", help="List the windows on the display (for tuning).")
    win.add_argument("--screen-backend", choices=("auto", "xdotool", "fake", "none"), default=None)
    commands.add_parser("prune", help="Apply the screenshot retention now.")
    commands.add_parser("show", help="Print the enrolled settings (no secrets).")
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        parser.print_help(out)
        return EXIT_USAGE
    if args.command == "enrol":
        return run_enrol(args, out)
    if args.command == "serve":
        return run_serve(args, out, once=bool(args.once))
    if args.command == "doctor":
        return run_doctor(args, out)
    if args.command == "windows":
        return run_windows(args, out)
    if args.command == "prune":
        return run_prune(args, out)
    return run_show(args, out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
