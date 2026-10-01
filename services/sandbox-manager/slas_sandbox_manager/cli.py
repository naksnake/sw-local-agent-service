"""`slas-sandbox-manager`: the container command (docs/api-contract-round-2.md §1).

    serve   read the settings from the environment, probe the runtime socket, start the reaper
            thread and serve on SLAS_BIND (uvicorn, 0.0.0.0:8000)

The sandbox images are handled by `python -m slas_sandbox_manager.images` (list · manifest ·
render), which install.sh runs on the bare host, so they are not repeated here.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from fastapi import FastAPI

from slas_http.serve import run
from slas_sandbox_manager.service.app import build_services, create_app
from slas_sandbox_manager.service.settings import Settings, SettingsError

Server = Callable[[FastAPI, str], None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slas-sandbox-manager", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="probe the runtime socket, then serve on SLAS_BIND")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    server: Server = run,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        settings = Settings.from_env(env)
    except SettingsError as exc:
        err.write(f"{exc}\n")
        return 2
    if args.command == "serve":
        services = build_services(settings)
        sentence = (
            services.isolation.sentence
            if services.isolation is not None
            else f"{services.runtime_problem} The service starts anyway and reports it on /health."
        )
        out.write(f"{sentence}\n")
        out.write(
            f"Toolchains from {services.manifest.source}; sandbox images tagged "
            f"{settings.registry}/slas/sandbox-<language>:<version>. Serving on {settings.bind}.\n"
        )
        server(create_app(settings, services=services, reaper=True), settings.bind)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
