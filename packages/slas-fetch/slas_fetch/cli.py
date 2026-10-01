"""The command line behind `scripts/fetch_models.py` (a thin wrapper over this module).

    fetch_models.py fetch  --sources config/model-sources.txt --profile quickstart --dest ./models
    fetch_models.py fetch  --sources config/model-sources.txt --profile prod --dry-run --dest ./m
    fetch_models.py fetch  --model qwen3.8-27b-fp8=Qwen/Qwen3.8-27B-FP8 --dest ./models
    fetch_models.py verify --dest /AI/Agent/Models [--model <path>]     # offline
    fetch_models.py merge-manifest --dest /AI/Agent/Models --from ./models/manifest.json

Before anything downloads, the fetch lists every model with its size and checks the free
disk at --dest, counting a partial download that will resume; `--dry-run` stops there. A
token for gated repositories comes from HF_TOKEN in the environment, never from argv;
HF_ENDPOINT points at a mirror inside the perimeter when one exists.

The sources file may carry `[quickstart]` and `[prod]` sections: `--profile quickstart`
fetches the lines above any section and the `[quickstart]` ones, `--profile prod` also the
`[prod]` ones. Without `--profile`, every line is fetched. A `# comment` may follow a line.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from slas_fetch.fetch import (
    DEFAULT_ENDPOINT,
    PROFILES,
    FetchedModel,
    FetchError,
    Hub,
    Opener,
    Source,
    already_here,
    check_disk,
    fetch_planned,
    human,
    merge_manifest,
    plan_model,
    read_sources,
    verify,
    write_manifest,
)

DESCRIPTION = "Fetch model weights on a CONNECTED host and write checksums for the air-gapped box."


def iter_sources(args: argparse.Namespace) -> Iterator[Source]:
    if args.sources:
        yield from read_sources(Path(args.sources), profile=args.profile)
    elif args.profile:
        raise FetchError(
            "--profile was given without --sources.",
            "A profile selects sections of a sources file; --model lines are always fetched.",
            "Add --sources config/model-sources.txt, or drop --profile.",
        )
    for spec in args.model or []:
        yield Source.parse(spec)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fetch_models.py", description=DESCRIPTION)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser(
        "fetch", help="Download models and write checksums (connected host)."
    )
    fetch.add_argument(
        "--sources", help="config/model-sources.txt: `<path> <owner/repo> [revision]` per line."
    )
    fetch.add_argument(
        "--profile",
        choices=PROFILES,
        help="Only the models this install profile needs, per the sections in --sources.",
    )
    fetch.add_argument(
        "--model", action="append", help="One model as <path>=<owner/repo>[@revision]; repeatable."
    )
    fetch.add_argument("--dest", required=True, help="Directory that becomes Models/ on the box.")
    fetch.add_argument(
        "--include", action="append", default=[], help="Only files matching this glob (repeatable)."
    )
    fetch.add_argument(
        "--exclude", action="append", default=[], help="Skip files matching this glob (repeatable)."
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="List every model with its size and check the free disk; download nothing.",
    )
    check = commands.add_parser(
        "verify", help="Check every SHA256SUMS under --dest; no network (the box)."
    )
    check.add_argument("--dest", required=True)
    check.add_argument(
        "--model",
        action="append",
        default=[],
        help="Only this model directory under --dest (repeatable); default: every one.",
    )
    merge = commands.add_parser(
        "merge-manifest",
        help="Fold another manifest.json into --dest/manifest.json (install.sh uses it).",
    )
    merge.add_argument("--dest", required=True)
    merge.add_argument("--from", dest="source", required=True, help="The manifest.json to fold in.")
    return parser


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, opener: Opener | None = None
) -> int:
    out = stdout if stdout is not None else sys.stdout
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    dest = Path(args.dest)
    try:
        if args.command == "verify":
            problems = verify(dest, log=out, only=args.model)
            if problems:
                out.write("\n".join(problems) + "\n")
                out.write(
                    "What to do: copy the listed files again from the build host and run "
                    "verify again.\n"
                )
                return 1
            scope = ", ".join(args.model) if args.model else f"Every model under {dest}"
            out.write(f"{scope} matches its checksums.\n")
            return 0
        if args.command == "merge-manifest":
            added, updated = merge_manifest(dest, Path(args.source), now=datetime.now(UTC))
            out.write(
                f"{dest / 'manifest.json'}: {added} models added, {updated} updated from "
                f"{args.source}.\n"
            )
            return 0
        sources = list(iter_sources(args))
        if not sources:
            raise FetchError(
                "No model was named.",
                "Neither --sources nor --model was given.",
                "Pass --sources config/model-sources.txt, or --model <path>=<owner/repo>.",
            )
        hub = Hub(
            endpoint=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
            token=os.environ.get("HF_TOKEN") or None,
        )
        if opener is not None:
            hub.opener = opener
        # A model an earlier run completed is kept without asking the hub or hashing a byte:
        # the manifest names the pinned commit and every file is there at its recorded size.
        kept: list[FetchedModel] = []
        todo: list[Source] = []
        for source in sources:
            done = already_here(dest, source)
            if done is None:
                todo.append(source)
                continue
            kept.append(done)
            out.write(
                f"{source.path}: already complete at {dest / source.path} ({len(done.files)} "
                f"files, {human(done.bytes)}); nothing to download.\n"
            )
        if not todo:
            if len(kept) == 1:
                out.write("The model is already here; nothing was downloaded.\n")
            else:
                out.write(f"All {len(kept)} models are already here; nothing was downloaded.\n")
            return 0
        # Say what will happen before it happens (CLAUDE.md §9): list and size everything,
        # check the disk, and only then download.
        plans = [
            plan_model(hub, source, dest, include=args.include, exclude=args.exclude, log=out)
            for source in todo
        ]
        for plan in plans:
            out.write(plan.sentence() + "\n")
        check_disk(plans, dest, log=out)
        if args.dry_run:
            out.write("Dry run: nothing was downloaded.\n")
            return 0
        fetched = [fetch_planned(hub, plan, dest, log=out) for plan in plans]
        manifest = write_manifest(dest, fetched, now=datetime.now(UTC))
        for model in fetched:
            out.write(model.sentence() + "\n")
        total = sum(m.bytes for m in fetched)
        noun = "model" if len(fetched) == 1 else "models"
        already = f" {len(kept)} already here, untouched." if kept else ""
        out.write(
            f"Done: {len(fetched)} {noun}, {human(total)}, checksums in each SHA256SUMS and "
            f"{manifest}.{already}\n"
            f"Next: on the platform host run `./install.sh --models {dest}` (carry {dest}/ "
            "there first if this is not that host); the installer verifies every checksum "
            "and puts the weights under Models/.\n"
        )
        return 0
    except FetchError as exc:
        out.write(exc.render() + "\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
