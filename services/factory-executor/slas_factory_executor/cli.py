"""`slas-factory-executor serve`: the Zone B-prime container command (CLAUDE.md §4.1, ADR-0015).

At start: the platform CA under `Factory/ca` (created with `openssl` when absent, ADR-0010)
and the executor's own certificate, the enrolment endpoint on ENROLMENT_LISTEN, the MES
poller thread over `Factory/mes`, the shipped template copied to `Factory/Templates`, and an
mTLS runner client per enrolled station. A station enrolled later is reachable without a
restart (INV-9). Without a station the service is healthy and idle.
"""

from __future__ import annotations

import argparse
import os
import ssl
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, TextIO

from fastapi import FastAPI

from slas_factory_executor.executor import FactoryExecutor
from slas_factory_executor.mes import FileDropMesAdapter
from slas_factory_executor.service.app import SERVICE, create_app
from slas_factory_executor.service.collaborators import (
    GatewayCrossChecker,
    MesPoller,
    RunnerTable,
    StationKeys,
    VncWatcher,
    Watcher,
    ensure_shipped_templates,
)
from slas_factory_executor.service.settings import (
    Settings,
    StartError,
    build_resolver,
    check_mes_adapter,
    load_factory_settings,
)
from slas_factory_executor.stations import (
    CertificateAuthority,
    EnrolmentServer,
    EnrolmentService,
    OpensslCa,
    StationError,
    StationRegistry,
)
from slas_hal.drivers.process import LocalProcessRunner
from slas_http.client import ServiceClient
from slas_http.serve import run as serve_app
from slas_kernel.clock import SystemClock
from slas_kernel.rca import CrossChecker
from slas_observability.events import EventLog, StreamSink
from slas_schemas.envfile import write_atomic

EXIT_OK: Final = 0
EXIT_PROBLEM: Final = 1
EXIT_USAGE: Final = 2
EXECUTOR_CN: Final = "factory-executor"

Runner = Callable[[FastAPI, str], None]


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(
        prog="slas-factory-executor", description=__doc__.splitlines()[0]
    )
    commands = parse.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the executor on SLAS_BIND (default 0.0.0.0:8000)")
    return parse


class EnrolmentState:
    """What `/health` says about the enrolment endpoint."""

    def __init__(self) -> None:
        self.state = "down"

    def __call__(self) -> str:
        return self.state


def executor_certificate(
    ca: CertificateAuthority, ca_dir: Path, hosts: Sequence[str]
) -> tuple[Path, Path]:
    """The executor's own certificate and key, issued once by the platform CA: it serves the
    enrolment endpoint and is the client certificate every runner sees (ADR-0010)."""
    certfile = ca_dir / "executor.pem"
    keyfile = ca_dir / "executor.key"
    if not (certfile.is_file() and keyfile.is_file()):
        issued = ca.issue_cert(EXECUTOR_CN, san_hosts=list(hosts))
        ca_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(certfile, issued.cert_pem, mode=0o600)
        write_atomic(keyfile, issued.key_pem, mode=0o600)
    return certfile, keyfile


def build_app(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
    log: EventLog | None = None,
    ca: CertificateAuthority | None = None,
) -> FastAPI:
    """Everything `serve` wires, without starting uvicorn (tests call this; `ca` lets them
    skip `openssl`)."""
    env = dict(os.environ if environ is None else environ)
    event_log = log if log is not None else EventLog(SERVICE, StreamSink())
    check_mes_adapter(settings)
    factory_settings = load_factory_settings(settings.factory_settings, event_log)
    resolver = build_resolver(settings, env)
    clock = SystemClock()

    # The CA and the executor's certificate: created at first start, nothing to set up by hand.
    ca_dir = settings.ca_dir
    if ca is None:
        openssl = OpensslCa(
            ca_cert=ca_dir / "ca.pem",
            ca_key=ca_dir / "ca.key",
            runner=LocalProcessRunner(),
            workdir=ca_dir / "work",
        )
        try:
            if openssl.ensure_ca():
                event_log.info("ca.created", path=str(ca_dir / "ca.pem"))
        except StationError as exc:
            raise StartError(exc.message) from None
        ca = openssl
    registry = StationRegistry(settings.factory_dir / "stations.json")
    enrolment = EnrolmentService(
        registry,
        ca=ca,
        clock=clock,
        data_root=settings.data_root,
        code_ttl_minutes=factory_settings.enrolment_code_ttl_minutes,
        max_attempts=factory_settings.enrolment_max_attempts,
    )

    enrolment_state = EnrolmentState()
    watcher: Watcher | None = None
    endpoint: EnrolmentServer | None = None
    try:
        certfile, keyfile = executor_certificate(ca, ca_dir, settings.enrolment_hosts)
        endpoint = EnrolmentServer(
            enrolment,
            bind=settings.enrolment_listen,
            certfile=str(certfile),
            keyfile=str(keyfile),
        )
        endpoint.start()
    except (StationError, OSError, ssl.SSLError) as exc:
        detail = exc.message.what_happened if isinstance(exc, StationError) else str(exc)[:200]
        event_log.warning("enrolment.not_listening", listen=settings.enrolment_listen, error=detail)
        runners = RunnerTable(
            registry,
            certfile=ca_dir / "executor.pem",
            keyfile=ca_dir / "executor.key",
            cafile=settings.runner_mtls_ca,
        )
    else:
        enrolment_state.state = "ok"
        runners = RunnerTable(
            registry, certfile=certfile, keyfile=keyfile, cafile=settings.runner_mtls_ca
        )
        watcher = VncWatcher(
            certfile=certfile,
            keyfile=keyfile,
            cafile=settings.runner_mtls_ca,
            listen=settings.vnc_tunnel_listen,
        )

    cross_checker: CrossChecker | None = None
    if settings.gateway_url:
        cross_checker = GatewayCrossChecker(
            ServiceClient("llm-gateway", settings.gateway_url), log=event_log
        )
    executor = FactoryExecutor(
        runners=runners,
        resolver=resolver,
        signing_key_ref=settings.batch_key_ref,
        signing_key_id=settings.batch_key_id,
        data_root=settings.data_root,
        clock=clock,
        cross_checker=cross_checker,
        lease_hours=factory_settings.station_lease_hours,
        station_keys=StationKeys(registry, enrolment),
    )

    mes = FileDropMesAdapter(settings.mes_dir)
    poller = MesPoller(mes, interval_s=settings.mes_poll_interval_s, log=event_log)
    poller.start()
    written = ensure_shipped_templates(settings.templates_dir)
    if written:
        event_log.info("templates.copied", files=[p.name for p in written])

    app = create_app(
        executor=executor,
        registry=registry,
        enrolment=enrolment,
        mes=mes,
        templates_dir=settings.templates_dir,
        leases=executor.leases,
        watcher=watcher,
        log=event_log,
        clock=clock,
        enrolment_state=enrolment_state,
        mes_state=poller.state,
    )
    app.state.enrolment_server = endpoint
    app.state.mes_poller = poller
    event_log.info(
        "factory_executor.started",
        stations=len(registry.list()),
        enrolled=sum(1 for r in registry.list() if r.enrolled),
        enrolment=enrolment_state(),
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
