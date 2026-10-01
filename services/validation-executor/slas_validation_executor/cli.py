"""`slas-validation-executor serve`: the Zone B container command (CLAUDE.md §4.1, ADR-0015).

At start: the syslog receiver on SYSLOG_LISTEN, credentials through `resolver_for`,
guardrails from GUARDRAIL_POLICY, BMC quirks from BMC_QUIRKS, `RealHal` over the target
registry the `slas target` commands write (`Validation/targets.json`), leases at
`Validation/leases.json`. Without a target the service is healthy and idle; the first plan
that names a target finds it in the registry, no restart needed (INV-9).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final, TextIO

from fastapi import FastAPI

from slas_hal.drivers.process import LocalProcessRunner, LocalStreamRunner
from slas_hal.drivers.real import RealHal
from slas_hal.drivers.syslog import SyslogReceiver
from slas_hal.http import UrllibHttpClient
from slas_hal.targets import TargetRegistry
from slas_http.serve import run as serve_app
from slas_kernel.clock import SystemClock
from slas_observability.events import EventLog, StreamSink
from slas_validation_executor.executor import ValidationExecutor
from slas_validation_executor.service.app import SERVICE, create_app
from slas_validation_executor.service.settings import (
    Settings,
    StartError,
    build_resolver,
    load_guardrails,
    load_quirks,
)

EXIT_OK: Final = 0
EXIT_PROBLEM: Final = 1
EXIT_USAGE: Final = 2

Runner = Callable[[FastAPI, str], None]


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(
        prog="slas-validation-executor", description=__doc__.splitlines()[0]
    )
    commands = parse.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the executor on SLAS_BIND (default 0.0.0.0:8000)")
    return parse


class SyslogState:
    """What `/health` says about the receiver: "ok" once it listens, "down" when the port
    could not be bound (the service still serves steps; fences then go to SOL only)."""

    def __init__(self) -> None:
        self.state = "down"

    def __call__(self) -> str:
        return self.state


def build_app(
    settings: Settings, *, environ: Mapping[str, str] | None = None, log: EventLog | None = None
) -> FastAPI:
    """Everything `serve` wires, without starting uvicorn (tests call this)."""
    env = dict(os.environ if environ is None else environ)
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    validation = settings.validation_dir
    registry = TargetRegistry(validation / "targets.json")
    guardrails = load_guardrails(settings.guardrail_policy, event_log)
    quirks = load_quirks(settings.bmc_quirks, event_log)
    resolver = build_resolver(settings, env)

    syslog = SyslogReceiver(
        listen=settings.syslog_listen, sink=validation / "Syslog" / "targets.jsonl"
    )
    syslog_state = SyslogState()
    try:
        syslog.start()
    except OSError as exc:
        event_log.warning(
            "syslog.not_listening", listen=settings.syslog_listen, error=str(exc)[:200]
        )
    else:
        syslog_state.state = "ok"

    clock = SystemClock()
    hal = RealHal(
        registry,
        http=UrllibHttpClient(),
        runner=LocalProcessRunner(),
        stream_runner=LocalStreamRunner(),
        resolver=resolver,
        clock=clock,
        data_root=settings.data_root,
        sleep=time.sleep,
        quirk_table=quirks,
        syslog=syslog,
    )
    executor = ValidationExecutor(
        hal=hal, data_root=settings.data_root, clock=clock, guardrails=guardrails
    )
    app = create_app(
        executor=executor,
        registry=registry,
        leases=executor.leases,
        log=event_log,
        clock=clock,
        syslog_state=syslog_state,
    )
    app.state.syslog = syslog
    event_log.info(
        "validation_executor.started",
        targets=len(registry.list()),
        syslog=syslog_state(),
        sentences=settings.sentences(),
    )
    return app


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    runner: Runner = serve_app,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        args = parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    settings = Settings.from_environ(environ)
    if args.command == "serve":
        try:
            app = build_app(settings, environ=environ)
        except StartError as exc:
            out.write(exc.message.render() + "\n")
            return EXIT_PROBLEM
        for sentence in settings.sentences():
            out.write(sentence + "\n")
        runner(app, settings.bind)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover — the console script calls main()
    sys.exit(main())
