"""`slas-orchestrator`: the container command (contract §1).

slas-orchestrator serve [--bind HOST:PORT]     settings from the environment, uvicorn
slas-orchestrator routes                       print the route table, one per line
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from slas_orchestrator.service.settings import Settings

PROG: Final = "slas-orchestrator"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Runs the Agent Kernel and hosts the Coding, Validation and Factory agents.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the HTTP service")
    serve.add_argument(
        "--bind",
        default=None,
        help="HOST:PORT to listen on (default: SLAS_BIND, or 0.0.0.0:8000 in the container)",
    )
    commands.add_parser("routes", help="print every route the service serves")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    settings = Settings.from_env()
    if args.command == "routes":
        from slas_orchestrator.service.app import create_app, route_table

        app = create_app(settings, gateway=None, broker=None)
        for method, path in route_table(app):
            sys.stdout.write(f"{method} {path}\n")
        return 0
    if args.command == "serve":  # pragma: no cover — starts a server
        from slas_http.serve import run
        from slas_orchestrator.service.app import create_app

        run(create_app(settings), args.bind or settings.bind)
        return 0
    return 2  # pragma: no cover — argparse refuses unknown commands first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
