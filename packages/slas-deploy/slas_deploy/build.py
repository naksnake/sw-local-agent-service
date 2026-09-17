"""`install.sh --build` (ADR-0014): on a connected host, pull the third-party images by their
pinned tags, build the first-party images from this checkout, and write a filled image lock
under the data root. The lock in git stays unpinned; the one written here is what
`check-lock` and `compose up --pull never` then use on this host.

    third-party   docker pull <upstream> · record its manifest digest and image ID ·
                  docker tag <upstream> <registry>/<reference>
    first-party   docker build -f images/<name>/Dockerfile -t <registry>/slas/<name>:<version> .
                  · record the image ID (there is no registry digest for a local build; the
                  lock counts a first-party image as pinned by its image ID)

One sentence per image. `docker` is called with argv lists only; a fake runner stands in
for it in tests, and install.sh's tests use a stub `docker` on PATH.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TextIO

from slas_deploy.dockerfiles import dockerfile_path
from slas_deploy.images import VERSION, ImageLock, LockedImage, Profile
from slas_schemas.errors import ThreePartMessage

_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class Completed:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Runner(Protocol):
    def run(self, argv: Sequence[str], *, capture: bool = True) -> Completed: ...


class LocalRunner:
    """subprocess with the caller's environment (docker needs DOCKER_HOST, HOME, proxies)."""

    def run(self, argv: Sequence[str], *, capture: bool = True) -> Completed:
        try:
            completed = subprocess.run(  # noqa: S603 — argv list, no shell
                list(argv), capture_output=capture, text=True, check=False
            )
        except FileNotFoundError:
            return Completed(127, "", f"{argv[0]}: not found on this host")
        return Completed(completed.returncode, completed.stdout or "", completed.stderr or "")


class BuildError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


@dataclass(frozen=True)
class Step:
    """What --dry-run prints and what a real run performs for one image."""

    image: LockedImage
    reference: str  # the compose reference with the registry and the version filled in
    argv: tuple[tuple[str, ...], ...]  # the docker commands, in order
    sentence: str  # the dry-run sentence

    @property
    def first_party(self) -> bool:
        return self.image.first_party


def compose_reference(image: LockedImage, *, registry: str, version: str) -> str:
    return f"{registry}/{image.reference}".replace(VERSION, version)


def plan(
    lock: ImageLock, profile: Profile, *, registry: str, version: str, repo: Path
) -> list[Step]:
    steps: list[Step] = []
    for image in lock.for_profile(profile):
        reference = compose_reference(image, registry=registry, version=version)
        if image.first_party:
            dockerfile = dockerfile_path(image.name)
            steps.append(
                Step(
                    image,
                    reference,
                    (
                        (
                            "docker",
                            "build",
                            "--file",
                            str(repo / dockerfile),
                            "--tag",
                            reference,
                            str(repo),
                        ),
                        ("docker", "image", "inspect", "--format", "{{.Id}}", reference),
                    ),
                    f"Would build {reference} from {dockerfile}.",
                )
            )
        else:
            steps.append(
                Step(
                    image,
                    reference,
                    (
                        ("docker", "pull", "--quiet", image.upstream),
                        (
                            "docker",
                            "image",
                            "inspect",
                            "--format",
                            "{{index .RepoDigests 0}}",
                            image.upstream,
                        ),
                        ("docker", "image", "inspect", "--format", "{{.Id}}", image.upstream),
                        ("docker", "tag", image.upstream, reference),
                    ),
                    f"Would pull {image.upstream} and tag it {reference}.",
                )
            )
    return steps


def _fail(step: Step, verb: str, result: Completed) -> BuildError:
    detail = (result.stderr or result.stdout).strip().splitlines()
    tail = " ".join(detail[-3:]) if detail else "no output"
    subject = step.reference if step.first_party else step.image.upstream
    return BuildError(
        ThreePartMessage(
            f"{verb} {subject} did not finish (docker exited {result.exit_code}: {tail}).",
            "The host lost its route to the registry, the build context is incomplete, or the "
            "Docker daemon is not running."
            if not step.first_party
            else "A build step failed: a base image could not be pulled, a dependency could not "
            "be fetched, or the Dockerfile and the source tree disagree.",
            "Read docker's messages above, fix what they name, then run ./install.sh --build "
            "again; images already built or pulled are kept.",
        )
    )


def _digest_from(text: str) -> str | None:
    match = _DIGEST.search(text)
    return match.group(0) if match else None


def build_images(
    lock: ImageLock,
    profile: Profile,
    *,
    registry: str,
    version: str,
    repo: Path,
    runner: Runner,
    out: TextIO,
) -> ImageLock:
    """Perform every step; return the lock with digests and image IDs filled in."""
    filled: list[LockedImage] = []
    planned = plan(lock, profile, registry=registry, version=version, repo=repo)
    steps = {step.image.name: step for step in planned}
    for image in lock.images:
        step = steps.get(image.name)
        if step is None:
            filled.append(image)  # another profile's image: left as it was
            continue
        if step.first_party:
            build = runner.run(step.argv[0], capture=False)
            if not build.ok:
                raise _fail(step, "Building", build)
            inspect = runner.run(step.argv[1])
            image_id = _digest_from(inspect.stdout)
            if not inspect.ok or image_id is None:
                raise _fail(step, "Inspecting", inspect)
            filled.append(image.model_copy(update={"digest": None, "image_id": image_id}))
            out.write(
                f"Built {step.reference} from {dockerfile_path(image.name)} "
                f"(image ID {image_id[:19]}…).\n"
            )
        else:
            pull = runner.run(step.argv[0], capture=False)
            if not pull.ok:
                raise _fail(step, "Pulling", pull)
            repo_digest = runner.run(step.argv[1])
            digest = _digest_from(repo_digest.stdout)
            if not repo_digest.ok or digest is None:
                raise _fail(step, "Inspecting", repo_digest)
            inspect = runner.run(step.argv[2])
            image_id = _digest_from(inspect.stdout)
            if not inspect.ok or image_id is None:
                raise _fail(step, "Inspecting", inspect)
            tag = runner.run(step.argv[3])
            if not tag.ok:
                raise _fail(step, "Tagging", tag)
            filled.append(image.model_copy(update={"digest": digest, "image_id": image_id}))
            out.write(f"Pulled {image.upstream} ({digest[:19]}…) and tagged it {step.reference}.\n")
        out.flush()
    return lock.model_copy(update={"images": filled})


def describe(steps: Sequence[Step], *, out: TextIO, lock_path: Path) -> None:
    for step in steps:
        out.write(step.sentence + "\n")
    built = sum(1 for step in steps if step.first_party)
    out.write(
        f"Would write the filled image lock to {lock_path} ({built} built, "
        f"{len(steps) - built} pulled); it is never committed.\n"
    )
