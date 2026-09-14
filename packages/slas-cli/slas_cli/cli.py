"""`slas` — the command line that mirrors the WebUI (CLAUDE.md §3).

Phase 0 implements `doctor`; Phase 6 adds `toolchain list|add`. Every other command from §3
is registered so that typing it gives a sentence about when it arrives instead of an
argparse error. Standard library only: this runs on the bare host before anything is
installed.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from slas_cli import __version__
from slas_cli.doctor.checks import DoctorSettings, describe_host, run_checks
from slas_cli.doctor.host import Host, RealHost
from slas_cli.doctor.report import exit_code, render_json, render_text, supports_unicode
from slas_kernel.branding import DEFAULT_DATA_ROOT, PRODUCT_NAME
from slas_sandbox_manager.toolchains import ToolchainError, add_toolchain, load_manifest

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2

# Commands from CLAUDE.md §3 that later phases bring (docs/DEVELOPMENT_PLAN.md).
NOT_YET: dict[str, str] = {
    "status": "Phase 11",
    "logs": "a later phase",
    "user": "Phase 1",
    "model": "Phase 3",
    "skill": "Phase 4",
    "backup": "a later phase",
    "upgrade": "a later phase",
}


def build_parser(environ: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slas",
        description=f"{PRODUCT_NAME} command line. Mirrors the WebUI.",
    )
    parser.add_argument("--version", action="version", version=f"slas {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    doctor = commands.add_parser(
        "doctor",
        help="Check this host before installing. Changes nothing.",
        description="Checks this host and prints a plain-language report. Nothing is changed "
        "and nothing is sent anywhere.",
    )
    doctor.add_argument(
        "--data-root",
        default=environ.get("SLAS_DATA_ROOT") or DEFAULT_DATA_ROOT,
        help="Where the platform keeps its data (default: $SLAS_DATA_ROOT or "
        f"{DEFAULT_DATA_ROOT}).",
    )
    doctor.add_argument(
        "--profile",
        choices=("quickstart", "prod"),
        default=environ.get("SLAS_PROFILE") or "quickstart",
        help="Deployment profile to check for (default: $SLAS_PROFILE or quickstart).",
    )
    doctor.add_argument("--json", action="store_true", help="Print the report as JSON.")
    doctor.add_argument(
        "--ascii", action="store_true", help="Use plain ASCII markers in the text report."
    )

    toolchain = commands.add_parser(
        "toolchain",
        help="List or add the offline toolchains the Coding Agent can use.",
        description="The Coding Agent picks the newest bundled version of each language "
        "unless a version is pinned; these commands show and extend the bundle.",
    )
    toolchain.add_argument(
        "--data-root",
        default=environ.get("SLAS_DATA_ROOT") or DEFAULT_DATA_ROOT,
        help="Where the platform keeps its data (default: $SLAS_DATA_ROOT or "
        f"{DEFAULT_DATA_ROOT}).",
    )
    toolchain_commands = toolchain.add_subparsers(dest="toolchain_command", metavar="<action>")
    toolchain_commands.add_parser(
        "list", help="Show every bundled toolchain and its newest version."
    )
    add = toolchain_commands.add_parser(
        "add", help="Copy a toolchain archive into the bundle and record its version."
    )
    add.add_argument("language", help="python, c, cpp, rust, shell, go, typescript or config")
    add.add_argument("version", help="The version the archive contains, for example 3.13.1")
    add.add_argument("archive", help="Path to the archive already copied onto this host")

    for name, phase in NOT_YET.items():
        later = commands.add_parser(name, help=f"Arrives in {phase}.")
        later.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def run_toolchain(args: argparse.Namespace, out: TextIO) -> int:
    data_root = Path(args.data_root)
    try:
        if args.toolchain_command == "add":
            out.write(
                add_toolchain(data_root, args.language, args.version, Path(args.archive)) + "\n"
            )
            return EXIT_OK
        manifest = load_manifest(data_root)
        out.write(f"Toolchains in the offline bundle ({manifest.source}):\n")
        for line in manifest.sentences():
            out.write(f"  {line}\n")
        out.write(
            "Leave the version empty in the wizard to get the newest; pin one to use it if the "
            "bundle has it.\n"
        )
        return EXIT_OK
    except ToolchainError as exc:
        out.write(
            f"{exc.message.what_happened}\n{exc.message.likely_cause}\n{exc.message.what_to_do}\n"
        )
        return EXIT_PROBLEMS


def run_doctor(args: argparse.Namespace, host: Host, out: TextIO) -> int:
    settings = DoctorSettings(data_root=args.data_root, profile=args.profile)
    facts = describe_host(host)
    results = run_checks(host, settings)
    if args.json:
        out.write(render_json(results, facts, settings))
    else:
        unicode = supports_unicode(out) and not args.ascii
        out.write(render_text(results, facts, settings, unicode=unicode))
    return exit_code(results)


def main(
    argv: Sequence[str] | None = None,
    *,
    host: Host | None = None,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    env = environ if environ is not None else os.environ
    parser = build_parser(env)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command is None:
        parser.print_help(out)
        return EXIT_USAGE
    if args.command == "doctor":
        return run_doctor(args, host if host is not None else RealHost(), out)
    if args.command == "toolchain":
        if args.toolchain_command is None:
            parser.parse_args(["toolchain", "--help"])  # prints help and exits
        return run_toolchain(args, out)
    phase = NOT_YET[args.command]
    out.write(
        f"`slas {args.command}` is not available yet. It arrives in {phase} of "
        "docs/DEVELOPMENT_PLAN.md.\n"
    )
    return EXIT_USAGE
