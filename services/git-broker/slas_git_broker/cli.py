"""`slas-git-broker`: the console script of the git-broker container (contract §1).

    serve    read the settings from the environment, build the app, serve on SLAS_BIND

A missing secret key or an unusable allowlist stops the start with three parts on stderr
and exit code 1, so `slas logs git-broker` says what to fix.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from fastapi import FastAPI

from slas_git.credentials import CredentialError
from slas_git.hosts import GitHostsError
from slas_git_broker.service.app import create_app_from_settings
from slas_git_broker.service.settings import Settings
from slas_http.serve import run as serve_app
from slas_schemas.errors import ThreePartMessage

EXIT_OK = 0
EXIT_PROBLEM = 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="slas-git-broker",
        description="git-broker: the only holder of Git credentials, the only route to a remote.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="serve the broker's HTTP surface on SLAS_BIND")
    return parser.parse_args(argv)


def explain(message: ThreePartMessage, err: TextIO) -> None:
    err.write(f"{message.what_happened}\n{message.likely_cause}\n{message.what_to_do}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Callable[[FastAPI, str], None] = serve_app,
    settings: Settings | None = None,
    err: TextIO | None = None,
) -> int:
    err = err if err is not None else sys.stderr
    args = parse_args(argv)
    if args.command == "serve":
        try:
            current = settings if settings is not None else Settings.from_environ()
            app = create_app_from_settings(current)
        except (CredentialError, GitHostsError) as exc:
            explain(exc.message, err)
            return EXIT_PROBLEM
        except ValueError as exc:
            explain(
                ThreePartMessage(
                    "git-broker did not start.",
                    str(exc),
                    "Fix the environment variable and start the service again.",
                ),
                err,
            )
            return EXIT_PROBLEM
        run(app, current.bind)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
