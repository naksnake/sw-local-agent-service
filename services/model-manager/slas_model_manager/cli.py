"""`slas-model-manager serve`: the container command (docs/api-contract-round-2.md §1).

Reads the settings from the environment, builds the app, starts the reconcile loop in its
thread and serves on `SLAS_BIND`. Argv only; nothing here reads a secret.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from fastapi import FastAPI

from slas_http import serve
from slas_model_manager.driver import RuntimeApi
from slas_model_manager.service.app import create_app
from slas_model_manager.service.settings import Settings

Runner = Callable[[FastAPI, str], None]


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        prog="slas-model-manager", description="the model manager of SW Local Agent Service"
    )
    commands = out.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="reconcile the vLLM instances and serve on SLAS_BIND")
    return out


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = serve.run,
    api: RuntimeApi | None = None,
    stdout: TextIO | None = None,
) -> int:
    args = parser().parse_args(list(argv) if argv is not None else None)
    out = stdout if stdout is not None else sys.stdout
    settings = Settings.from_env(environ if environ is not None else os.environ)
    if args.command == "serve":
        app = create_app(settings, api=api)
        app.state.controller.start_loop(settings.reconcile_interval_s)
        out.write(
            f"Serving the model manager on {settings.bind}; reconciling every "
            f"{settings.reconcile_interval_s:g} s from {settings.models_file}.\n"
        )
        runner(app, settings.bind)
        return 0
    return 2  # pragma: no cover — argparse refuses unknown commands first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
