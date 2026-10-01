"""`install.sh --build`, the sandbox half (ADR-0014, ADR-0015; contract round 2 §4 and §9):
build the Coding Agent's sandbox images and write the toolchain manifest they satisfy.

The sandbox manager owns the list — `images/sandbox-<language>/Dockerfile` and the
toolchain each one carries — and publishes it through two commands the installer calls
with argv lists only:

    python -m slas_sandbox_manager.images list --registry <label>   name<TAB>tag<TAB>dockerfile
    python -m slas_sandbox_manager.images manifest --out <path>     the toolchain manifest

For every line: `docker build --file <repo>/<dockerfile> --tag <tag> <repo>` (the repository
root is the context, as for every first-party image), then the image ID is recorded in
`${SLAS_DATA_ROOT}/sandbox-images.lock.json`, a sibling of the filled image lock that is never
committed either. The sandbox images stay out of `compose/images.lock.*`: compose never starts
one; the sandbox manager does, by tag, over the runtime socket.

A dry run still asks for the list (read-only) and prints what it would build. When the
listing command is not available — a checkout where the sandbox manager's round has not
landed — the dry run says so and goes on; a real run stops in three parts, because a stack
without sandbox images cannot run a coding task.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, TextIO

from slas_deploy.build import BuildError, Completed, Runner
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage

LISTING_MODULE: Final = "slas_sandbox_manager.images"
LOCK_NAME: Final = "sandbox-images.lock.json"
MANIFEST_RELATIVE: Final = "Toolchains/manifest.json"
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_TAG: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SandboxImage:
    """One line of `images list`: the image name, its full tag and the Dockerfile."""

    name: str
    tag: str
    dockerfile: str  # relative to the repository root


class ListingUnavailableError(BuildError):
    """The listing command did not answer: the sandbox manager's round has not landed here."""


def list_command(python: str, registry: str) -> tuple[str, ...]:
    return (python, "-m", LISTING_MODULE, "list", "--registry", registry)


def manifest_command(python: str, out: Path) -> tuple[str, ...]:
    return (python, "-m", LISTING_MODULE, "manifest", "--out", str(out))


def parse_listing(text: str) -> list[SandboxImage]:
    """`name<TAB>tag<TAB>dockerfile` per line; blank lines and `#` comments are skipped."""
    images: list[SandboxImage] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise BuildError(
                ThreePartMessage(
                    f"Line {number} of the sandbox image list is not `name<TAB>tag<TAB>"
                    f"dockerfile`: {line!r}.",
                    f"`python -m {LISTING_MODULE} list` printed something the installer does "
                    "not understand.",
                    "The two sides must agree on docs/api-contract-round-2.md §4; report the "
                    "line above.",
                )
            )
        name, tag, dockerfile = (part.strip() for part in parts)
        if not _TAG.match(tag) or tag.endswith(":latest"):
            raise BuildError(
                ThreePartMessage(
                    f"The sandbox image {name} has the tag {tag!r}, which is not a pinned tag.",
                    "Every image needs `<registry>/<repository>:<tag>` and never `latest` (INV-8).",
                    "Fix the tag the sandbox manager prints for it.",
                )
            )
        images.append(SandboxImage(name, tag, dockerfile))
    return images


def list_sandbox_images(runner: Runner, *, python: str, registry: str) -> list[SandboxImage]:
    result = runner.run(list_command(python, registry))
    if not result.ok or not result.stdout.strip():
        # A module without the `list` command exits 0 and prints nothing: the same situation.
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else "printed nothing"
        raise ListingUnavailableError(
            ThreePartMessage(
                f"The sandbox image list could not be read (`python -m {LISTING_MODULE} list` "
                f"exited {result.exit_code}: {tail}).",
                "This checkout's sandbox manager does not publish its image list yet, or the "
                "locked Python environment is incomplete.",
                "Update the checkout to a revision where services/sandbox-manager ships "
                "`images list|manifest`, run `uv sync --frozen`, then run ./install.sh --build "
                "again.",
            )
        )
    return parse_listing(result.stdout)


def build_argv(image: SandboxImage, *, repo: Path) -> tuple[str, ...]:
    return (
        "docker",
        "build",
        "--file",
        str(repo / image.dockerfile),
        "--tag",
        image.tag,
        str(repo),
    )


def inspect_argv(image: SandboxImage) -> tuple[str, ...]:
    return ("docker", "image", "inspect", "--format", "{{.Id}}", image.tag)


def _fail(image: SandboxImage, verb: str, result: Completed) -> BuildError:
    detail = (result.stderr or result.stdout).strip().splitlines()
    tail = " ".join(detail[-3:]) if detail else "no output"
    return BuildError(
        ThreePartMessage(
            f"{verb} the sandbox image {image.tag} did not finish (docker exited "
            f"{result.exit_code}: {tail}).",
            "A build step failed: the pinned toolchain image or package could not be fetched, "
            "or the Dockerfile and the source tree disagree.",
            "Read docker's messages above, fix what they name, then run ./install.sh --build "
            "again; images already built are kept.",
        )
    )


@dataclass(frozen=True)
class BuiltSandboxImage:
    name: str
    tag: str
    dockerfile: str
    image_id: str


def build_sandbox_images(
    images: Sequence[SandboxImage], *, repo: Path, runner: Runner, out: TextIO
) -> list[BuiltSandboxImage]:
    built: list[BuiltSandboxImage] = []
    for image in images:
        result = runner.run(build_argv(image, repo=repo), capture=False)
        if not result.ok:
            raise _fail(image, "Building", result)
        inspect = runner.run(inspect_argv(image))
        match = _DIGEST.search(inspect.stdout)
        if not inspect.ok or match is None:
            raise _fail(image, "Inspecting", inspect)
        built.append(BuiltSandboxImage(image.name, image.tag, image.dockerfile, match.group(0)))
        out.write(f"Built {image.tag} from {image.dockerfile} (image ID {match.group(0)[:19]}…).\n")
        out.flush()
    return built


def render_lock(built: Sequence[BuiltSandboxImage], *, registry: str) -> str:
    document = {
        "version": 1,
        "registry": registry,
        "note": (
            "Sandbox images built by install.sh --build (ADR-0014); not compose services and not "
            "in compose/images.lock.*. The sandbox manager starts them by tag. Never committed."
        ),
        "images": [asdict(image) for image in built],
    }
    return json.dumps(document, indent=2) + "\n"


def write_manifest(runner: Runner, *, python: str, manifest_out: Path, out: TextIO) -> None:
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    result = runner.run(manifest_command(python, manifest_out))
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise BuildError(
            ThreePartMessage(
                f"Writing the toolchain manifest to {manifest_out} did not finish "
                f"(`python -m {LISTING_MODULE} manifest` exited {result.exit_code}: "
                f"{detail[-1] if detail else 'no output'}).",
                "The directory is not writable, or the sandbox manager's manifest command failed.",
                f"Check that {manifest_out.parent} belongs to you, then run ./install.sh --build "
                "again.",
            )
        )
    out.write(
        f"Wrote the toolchain manifest to {manifest_out}: the languages and versions the "
        "sandbox images carry, which the wizard's toolchain choices are resolved against.\n"
    )


def describe(
    images: Sequence[SandboxImage], *, out: TextIO, lock_path: Path, manifest_path: Path
) -> None:
    for image in images:
        out.write(f"Would build the sandbox image {image.tag} from {image.dockerfile}.\n")
    out.write(
        f"Would write the sandbox image lock to {lock_path} ({len(images)} built) and the "
        f"toolchain manifest to {manifest_path}; neither is committed.\n"
    )


def run(
    *,
    python: str,
    registry: str,
    repo: Path,
    lock_out: Path,
    manifest_out: Path,
    dry_run: bool,
    runner: Runner,
    out: TextIO,
) -> int:
    """The whole step; 0 when it finished (or, in a dry run, when it was described), 1 with a
    three-part message otherwise."""
    try:
        images = list_sandbox_images(runner, python=python, registry=registry)
    except ListingUnavailableError as exc:
        if dry_run:
            out.write(
                f"{exc.message.what_happened} In a real run this stops the install; the sandbox "
                f"images would be built from images/sandbox-*/Dockerfile and the toolchain "
                f"manifest written to {manifest_out}.\n"
            )
            return 0
        out.write(exc.message.render() + "\n")
        return 1
    except BuildError as exc:
        out.write(exc.message.render() + "\n")
        return 1
    if not images:
        out.write(
            "The sandbox manager lists no sandbox image (only comments), so none is built; the "
            "Coding Agent has no language to work in until one is added.\n"
        )
        return 0
    if dry_run:
        describe(images, out=out, lock_path=lock_out, manifest_path=manifest_out)
        return 0
    try:
        built = build_sandbox_images(images, repo=repo, runner=runner, out=out)
        lock_out.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(lock_out, render_lock(built, registry=registry), mode=0o644)
        out.write(f"Wrote the sandbox image lock to {lock_out}: {len(built)} images built.\n")
        write_manifest(runner, python=python, manifest_out=manifest_out, out=out)
    except BuildError as exc:
        out.write(exc.message.render() + "\n")
        return 1
    return 0
