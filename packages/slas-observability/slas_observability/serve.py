"""`python -m slas_observability.serve <service>` — `/health` and `/metrics` for a service in
the foreground, stopping cleanly on SIGTERM. Standard library only.

Every first-party service container answers the compose healthcheck and the Prometheus
scrape with this until its own entrypoint lands (CLAUDE.md §8.2, ADR-0014); a service's
round replaces the CMD and keeps the same `MetricsServer`. The bind address defaults to
`0.0.0.0:8000`, the port the compose file probes and Prometheus scrapes.
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from types import FrameType
from typing import Final, TextIO

from slas_observability.exposition import MetricsServer

DEFAULT_BIND: Final = "0.0.0.0:8000"  # the container's own address, as compose probes it
_SERVICE_NAME: Final = re.compile(r"^[a-z][a-z0-9-]*$")


def serve(
    service: str,
    *,
    bind: str = DEFAULT_BIND,
    stop: threading.Event | None = None,
    ready: Callable[[int], None] | None = None,
    out: TextIO | None = None,
) -> int:
    """Run the server until `stop` is set; `ready` receives the bound port once listening."""
    if not _SERVICE_NAME.match(service):
        raise ValueError(f"{service!r} is not a service name (lowercase letters, digits, dashes)")
    stream = out if out is not None else sys.stdout
    stop_event = stop if stop is not None else threading.Event()
    server = MetricsServer(service, bind=bind)
    server.start()
    host = bind.rpartition(":")[0] or "0.0.0.0"  # noqa: S104
    stream.write(
        f"{service}: answering /health and /metrics on {host}:{server.port}; "
        "the service's own entrypoint arrives in a later round.\n"
    )
    stream.flush()
    if ready is not None:
        ready(server.port)
    try:
        stop_event.wait()
    finally:
        server.stop()
    stream.write(f"{service}: stopped.\n")
    stream.flush()
    return 0


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m slas_observability.serve",
        description="Serve /health and /metrics for a service until SIGTERM or SIGINT.",
    )
    parser.add_argument("service", help="the service name, as compose and Prometheus know it")
    parser.add_argument("--bind", default=DEFAULT_BIND, help=f"host:port (default {DEFAULT_BIND})")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not _SERVICE_NAME.match(args.service):
        parser.error(f"{args.service!r} is not a service name (lowercase letters, digits, dashes)")

    stop = threading.Event()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, on_signal)
    return serve(args.service, bind=args.bind, stop=stop, out=stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
