"""`slas-api`: the in-container commands (docs/api-contract.md, ADR-0007).

`migrate` · `bootstrap status` · `user add` · `user list` · `serve`. Argv only; a password
travels on stdin (`--password-stdin`) or is generated here and printed once. Every command
acts as the `SYSTEM` principal with `via=cli` and writes the same audit rows as the routes.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from slas_api import service
from slas_api.app import build_services, create_app
from slas_api.db import migrate, session_scope
from slas_api.errors import ApiError
from slas_api.service import Services
from slas_api.settings import Settings
from slas_authz import SYSTEM
from slas_observability import tracing


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slas-api", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply the database migrations, then the bootstrap")

    bootstrap = commands.add_parser("bootstrap", help="the bootstrap administrator")
    bootstrap_commands = bootstrap.add_subparsers(dest="bootstrap_command", required=True)
    bootstrap_commands.add_parser("status", help="print pending or done")

    user = commands.add_parser("user", help="people")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    add = user_commands.add_parser("add", help="add a person")
    add.add_argument("--email", required=True)
    add.add_argument("--display-name", required=True)
    add.add_argument("--role", required=True)
    add.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of generating a one-time password",
    )
    user_commands.add_parser("list", help="one line per person")

    commands.add_parser("serve", help="migrate, bootstrap, then serve on the bind address")
    return parser


def _migrate(svc: Services, out: TextIO) -> int:
    dialect = migrate(svc.engine, svc.log)
    out.write(f"Migrations are up to date ({dialect}).\n")
    with tracing.trace():
        out.write(service.bootstrap(svc) + "\n")
    return 0


def _bootstrap_status(svc: Services, out: TextIO) -> int:
    out.write(service.bootstrap_status(svc) + "\n")
    return 0


def _user_add(svc: Services, args: argparse.Namespace, stdin: TextIO, out: TextIO) -> int:
    password: str | None = None
    if args.password_stdin:
        password = stdin.readline().rstrip("\r\n")
    with tracing.trace(), session_scope(svc.engine) as db:
        person, one_time = service.add_person(
            svc,
            db,
            actor=SYSTEM,
            via="cli",
            email=args.email,
            display_name=args.display_name,
            role=args.role,
            password=password,
        )
        label = svc.roles.current().describe(person.role).split(":", 1)[0]
        if one_time is None:
            out.write(
                f"{person.display_name} ({person.email}) can sign in as {label} with the "
                "password you provided.\n"
            )
        else:
            out.write(
                f"{person.display_name} ({person.email}) can sign in as {label} with the "
                f"one-time password: {one_time}\n"
                "It works once; they choose their own at first sign-in. "
                "You won't see it again.\n"
            )
    return 0


def _user_list(svc: Services, out: TextIO) -> int:
    with session_scope(svc.engine) as db:
        people = service.list_people(db)
        if not people:
            out.write("Nobody is registered yet; `slas-api migrate` creates the administrator.\n")
            return 0
        for person in people:
            state = "can sign in" if person.is_active else "switched off"
            if person.is_active and person.must_change_password:
                state = "must choose a password"
            last = service.format_ts(person.last_sign_in_at) or "never"
            out.write(f"{person.email}\t{person.role}\t{state}\tlast sign-in {last}\n")
    return 0


def _serve(svc: Services, settings: Settings) -> None:
    import uvicorn

    host, _, port = settings.slas_bind.rpartition(":")
    app = create_app(settings, services=svc)
    uvicorn.run(app, host=host or "0.0.0.0", port=int(port or 8000), log_level="warning")  # noqa: S104


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    settings: Settings | None = None,
) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    resolved = settings if settings is not None else Settings()
    svc = build_services(resolved)
    try:
        if args.command == "migrate":
            return _migrate(svc, out)
        if args.command == "bootstrap":
            return _bootstrap_status(svc, out)
        if args.command == "user" and args.user_command == "add":
            return _user_add(svc, args, inp, out)
        if args.command == "user":
            return _user_list(svc, out)
        _migrate(svc, out)
        _serve(svc, resolved)
        return 0
    except ApiError as exc:
        err.write(exc.message.render() + "\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
