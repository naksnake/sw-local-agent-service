"""`slas-model-fetcher serve`: the container command (docs/api-contract-round-2.md §1).

Reads the settings from the environment, builds the app and serves on `SLAS_BIND`. Argv
only; the one secret (the hub token) is read from its file at fetch time, never here.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from fastapi import FastAPI

from slas_http import serve
from slas_model_fetcher.service.app import create_app
from slas_model_fetcher.service.settings import Settings

Runner = Callable[[FastAPI, str], None]


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        prog="slas-model-fetcher",
        description="the model fetcher of SW Local Agent Service (quickstart only, ADR-0018)",
    )
    commands = out.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the fetch routes on SLAS_BIND")
    return out


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = serve.run,
    stdout: TextIO | None = None,
) -> int:
    args = parser().parse_args(list(argv) if argv is not None else None)
    out = stdout if stdout is not None else sys.stdout
    settings = Settings.from_env(environ if environ is not None else os.environ)
    if args.command == "serve":
        app = create_app(settings)
        out.write(f"Serving the model fetcher on {settings.bind}. {settings.sentence()}\n")
        runner(app, settings.bind)
        return 0
    return 2  # pragma: no cover — argparse refuses unknown commands first


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
