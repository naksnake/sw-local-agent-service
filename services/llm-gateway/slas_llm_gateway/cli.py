"""`slas-gateway`: the container command of the llm-gateway (contract round 2 §1).

`serve` reads the settings from the environment, builds the app and serves it on
`SLAS_BIND`. A settings or rules file that cannot be used stops the start with three parts
on stderr and exit code 1; nothing is guessed about a redaction rule (INV-5).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from fastapi import FastAPI

from slas_http import serve
from slas_llm_gateway.service.app import create_app
from slas_llm_gateway.service.settings import Settings, SettingsError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slas-gateway", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the gateway on SLAS_BIND (default 0.0.0.0:8000)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    err: TextIO = sys.stderr,
    run: Callable[[FastAPI, str], None] = serve.run,
) -> int:
    args = _parser().parse_args(argv)
    if args.command != "serve":  # pragma: no cover — argparse refuses anything else
        return 2
    try:
        settings = Settings.from_env()
        app = create_app(settings=settings)
    except SettingsError as exc:
        err.write(
            f"{exc.message.what_happened}\n{exc.message.likely_cause}\n{exc.message.what_to_do}\n"
        )
        return 1
    run(app, settings.bind)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
