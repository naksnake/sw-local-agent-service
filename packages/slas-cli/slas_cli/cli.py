"""Argument parsing for `slas` (standard library only; see the package docstring)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from slas_cli import __version__
from slas_cli.doctor import Thresholds, run_doctor
from slas_cli.probe import RealHostProbe
from slas_cli.report import render_json, render_text

DEFAULT_DATA_ROOT = "/AI/Agent"  # CLAUDE.md §0.1; mirrored from slas_kernel.branding
PRODUCT_NAME = "SW Local Agent Service"

# Subcommands CLAUDE.md §3 promises, with the phase that delivers each (docs/DEVELOPMENT_PLAN.md).
LATER_COMMANDS: dict[str, str] = {
    "status": "P11",
    "logs": "P1",
    "user": "P1",
    "model": "P3",
    "toolchain": "P6",
    "skill": "P4",
    "backup": "P1",
    "upgrade": "P1",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slas",
        description=f"{PRODUCT_NAME} command line. It mirrors the WebUI.",
    )
    parser.add_argument("--version", action="version", version=f"slas {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    doctor = sub.add_parser(
        "doctor",
        help="check this host before installing and print a plain-language report",
    )
    doctor.add_argument(
        "--data-root",
        default=DEFAULT_DATA_ROOT,
        help=f"where platform data will live (default {DEFAULT_DATA_ROOT})",
    )
    doctor.add_argument(
        "--profile",
        default="quickstart",
        choices=["quickstart", "prod"],
        help="deployment profile (CLAUDE.md §3)",
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output for CI")
    doctor.add_argument("--min-cpus", type=int, default=Thresholds.min_cpus)
    doctor.add_argument("--min-memory-gib", type=int, default=Thresholds.min_memory_gib)
    doctor.add_argument("--min-disk-gib", type=int, default=Thresholds.min_disk_gib)
    doctor.add_argument("--edge-port", type=int, default=Thresholds.edge_port)

    for name, phase in LATER_COMMANDS.items():
        sub.add_parser(name, help=f"arrives in phase {phase}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command in LATER_COMMANDS:
        phase = LATER_COMMANDS[args.command]
        sys.stderr.write(
            f"`slas {args.command}` is not available yet. It arrives in phase {phase} of "
            "docs/DEVELOPMENT_PLAN.md; only `slas doctor` exists in this skeleton.\n"
        )
        return 2
    thresholds = Thresholds(
        min_cpus=args.min_cpus,
        min_memory_gib=args.min_memory_gib,
        min_disk_gib=args.min_disk_gib,
        edge_port=args.edge_port,
    )
    report = run_doctor(
        RealHostProbe(),
        data_root=args.data_root,
        profile=args.profile,
        thresholds=thresholds,
        product=PRODUCT_NAME,
    )
    sys.stdout.write(render_json(report) if args.json else render_text(report))
    return report.exit_code
