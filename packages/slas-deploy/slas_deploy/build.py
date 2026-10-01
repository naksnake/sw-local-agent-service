"""`install.sh --build` (ADR-0014): on a connected host, pull the third-party images by their
pinned tags, build the first-party images from this checkout, and write a filled image lock
under the data root. The lock in git stays unpinned; the one written here is what
`check-lock` and `compose up --pull never` then use on this host.

    third-party   docker pull <upstream> (by digest when the lock records one) · record its
                  manifest digest and image ID · docker tag <upstream> <registry>/<reference>
                  The vLLM image is pulled and tagged like the rest; compose never starts it,
                  the model manager does (SLAS_VLLM_IMAGE).
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
    lock: ImageLock,
    profile: Profile,
    *,
    registry: str,
    version: str,
    repo: Path,
    agents: Sequence[str] | None = None,
) -> list[Step]:
    steps: list[Step] = []
    for image in lock.for_profile(profile, agents):
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
            pulled = image.pull_reference  # by digest when the lock knows it (INV-8)
            note = (
                " The model manager starts it per role and voter; compose never does."
                if image.started_by == "model-manager"
                else ""
            )
            if image.mirrors:
                note += (
                    f" Should {pulled.split('/', 1)[0]} refuse it, would pull the same release "
                    f"from {', '.join(image.mirrors)}."
                )
            steps.append(
                Step(
                    image,
                    reference,
                    (
                        ("docker", "pull", "--quiet", pulled),
                        (
                            "docker",
                            "image",
                            "inspect",
                            "--format",
                            "{{index .RepoDigests 0}}",
                            pulled,
                        ),
                        ("docker", "image", "inspect", "--format", "{{.Id}}", pulled),
                        ("docker", "tag", pulled, reference),
                    ),
                    f"Would pull {pulled} and tag it {reference}.{note}",
                )
            )
    return steps


def _fail(step: Step, verb: str, result: Completed) -> BuildError:
    tail = _tail(result, 3)
    subject = step.reference if step.first_party else step.image.pull_reference
    refused = any(
        marker in tail.lower() for marker in ("401", "unauthorized", "denied", "not found")
    )
    if step.first_party:
        cause = (
            "A build step failed: a base image could not be pulled, a dependency could not be "
            "fetched, or the Dockerfile and the source tree disagree."
        )
        what_to_do = (
            "Read docker's messages above, fix what they name, then run ./install.sh --build "
            "again; images already built or pulled are kept."
        )
    elif verb == "Pulling" and refused:
        cause = (
            "The registry refused the reference: the tag was removed or renamed upstream, or "
            "the registry now wants a login for it (quay.io answers 401 to both). Nothing on "
            "this host is at fault."
        )
        what_to_do = (
            "On a host that pulled this image before, the copy in Docker's store is reused "
            "and the install goes on; here there is none. Load the image from the bundle or "
            "another host (docker save | docker load), or pin a tag the registry still serves "
            "in slas_deploy.images (a reviewed change), then run ./install.sh --build again."
        )
    else:
        cause = (
            "The host lost its route to the registry, the build context is incomplete, or the "
            "Docker daemon is not running."
        )
        what_to_do = (
            "Read docker's messages above, fix what they name, then run ./install.sh --build "
            "again; images already built or pulled are kept."
        )
    return BuildError(
        ThreePartMessage(
            f"{verb} {subject} did not finish (docker exited {result.exit_code}: {tail}).",
            cause,
            what_to_do,
        )
    )


def _digest_from(text: str) -> str | None:
    match = _DIGEST.search(text)
    return match.group(0) if match else None


def _tail(result: Completed, lines: int) -> str:
    detail = (result.stderr or result.stdout).strip().splitlines()
    return " ".join(detail[-lines:]) if detail else "no output"


def _echo(out: TextIO, result: Completed) -> None:
    """docker's own words, as a run without capture would have shown them."""
    for text in (result.stdout, result.stderr):
        if text.strip():
            out.write(text if text.endswith("\n") else text + "\n")


def _inspect_id(reference: str) -> tuple[str, ...]:
    return ("docker", "image", "inspect", "--format", "{{.Id}}", reference)


def build_images(
    lock: ImageLock,
    profile: Profile,
    *,
    registry: str,
    version: str,
    repo: Path,
    runner: Runner,
    out: TextIO,
    agents: Sequence[str] | None = None,
) -> ImageLock:
    """Perform every step; return the lock with digests and image IDs filled in."""
    filled: list[LockedImage] = []
    planned = plan(lock, profile, registry=registry, version=version, repo=repo, agents=agents)
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
            # The first host: quay.io answered 401 for the MinIO tag it had served a day
            # earlier, and the install stopped although the same release sits on docker.io
            # and Docker's store still held the image from the previous install. So: the
            # upstream, then each mirror, then the copy already in the store (under the
            # upstream's name, or under the compose name a pull by digest left it with).
            # docker's output is captured so the sentence can carry the registry's answer,
            # and echoed so the person still sees it.
            pulled: str | None = None
            attempts: list[Completed] = []
            for candidate in image.pull_candidates:
                pull = runner.run(("docker", "pull", "--quiet", candidate))
                _echo(out, pull)
                if pull.ok:
                    pulled = candidate
                    break
                attempts.append(pull)
            from_store = False
            tag_needed = True
            if pulled is None:
                for present in (image.pull_reference, step.reference):
                    found = runner.run(_inspect_id(present))
                    if found.ok and _digest_from(found.stdout) is not None:
                        pulled, from_store = present, True
                        tag_needed = present != step.reference
                        break
            if pulled is None:
                raise _fail(step, "Pulling", attempts[-1])
            if attempts:
                failed = attempts[0]
                how = (
                    "the copy already in Docker's store is used"
                    if from_store
                    else f"pulled the same release from its mirror {pulled} instead"
                )
                out.write(
                    f"Pulling {image.pull_reference} did not finish (docker exited "
                    f"{failed.exit_code}: {_tail(failed, 2)}); {how}.\n"
                )
            repo_digest = runner.run(
                ("docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", pulled)
            )
            digest = _digest_from(repo_digest.stdout)
            if not repo_digest.ok or digest is None:
                raise _fail(step, "Inspecting", repo_digest)
            inspect = runner.run(_inspect_id(pulled))
            image_id = _digest_from(inspect.stdout)
            if not inspect.ok or image_id is None:
                raise _fail(step, "Inspecting", inspect)
            if tag_needed:
                tag = runner.run(("docker", "tag", pulled, step.reference))
                if not tag.ok:
                    raise _fail(step, "Tagging", tag)
            if image.digest and digest != image.digest:
                raise BuildError(
                    ThreePartMessage(
                        f"{image.pull_reference} came back with digest {digest[:19]}…, not the "
                        f"{image.digest[:19]}… the lock records.",
                        "The registry served a different manifest than the one the lock pins.",
                        "Check the registry mirror in use; the lock's digest changes only with "
                        "a reviewed change to slas_deploy.images.",
                    )
                )
            filled.append(image.model_copy(update={"digest": digest, "image_id": image_id}))
            if not tag_needed:
                out.write(f"Kept {pulled} ({digest[:19]}…) from Docker's store.\n")
            else:
                verb = "Kept" if from_store else "Pulled"
                out.write(f"{verb} {pulled} ({digest[:19]}…) and tagged it {step.reference}.\n")
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
