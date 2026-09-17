"""The image lock (ADR-0003, INV-8): every image the compose files reference, pinned by an
immutable tag, with the upstream manifest digest and the image ID the installer verifies.

    compose/images.lock.yaml   for people
    compose/images.lock.json   the same data for `install.sh` (PyYAML is not approved)

A digest of `null` means "not pinned yet": the lock is filled on a connected build host by
`scripts/lock-images.sh` (pull by tag, record digest and ID, sign with cosign). `install.sh`
refuses to load or pull an image the lock does not pin, so an unpinned lock cannot start a
platform — INV-8 is held by refusal, never relaxed. First-party images are built offline
and carry the release version as their tag; the bundle manifest records their IDs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

REGISTRY: Final = "${SLAS_REGISTRY}"
VERSION: Final = "${SLAS_VERSION}"
DEFAULT_REGISTRY: Final = "registry.internal"
PROD_REGISTRY_HINT: Final = "harbor.internal"

_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_LATEST: Final = re.compile(r"(:latest$|^[^:@]+$)")

Profile = Literal["quickstart", "prod"]
ALL_PROFILES: Final[tuple[Profile, ...]] = ("quickstart", "prod")
#: Who starts a locked image: compose (a service in the compose files) or the model manager
#: (the vLLM image, one container per role and voter; CLAUDE.md §7, contract round 2 §3).
StartedBy = Literal["compose", "model-manager"]


class LockedImage(SlasModel):
    #: The compose-side name (`postgres`) or the first-party service name.
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    #: What compose references, without the registry: `library/postgres:16.6`.
    reference: str = Field(min_length=3)
    #: Upstream source the build host pulls from, for the record.
    upstream: str = Field(min_length=3)
    first_party: bool = False
    #: Upstream manifest digest; None until the lock is filled on a build host. An image whose
    #: digest is known up front is pulled by that digest, never by its tag alone (INV-8).
    digest: str | None = None
    #: The image ID (config digest) `docker inspect` reports after load or pull.
    image_id: str | None = None
    #: Which cosign key signed it (`config/cosign.pub`): release images only.
    signed_by: str | None = None
    #: Profiles that start the image.
    profiles: list[Profile] = Field(default_factory=lambda: list(ALL_PROFILES))
    #: `compose` for a service in the compose files; `model-manager` for an image the model
    #: manager starts over the runtime socket and compose never references as a service.
    started_by: StartedBy = "compose"

    @property
    def pull_reference(self) -> str:
        """What a connected host pulls: `<upstream repository>@<digest>` when the lock knows
        the digest, the pinned upstream tag otherwise."""
        if self.digest and not self.first_party:
            repository = self.upstream.split("@", 1)[0].rsplit(":", 1)[0]
            return f"{repository}@{self.digest}"
        return self.upstream

    @property
    def pinned(self) -> bool:
        """Third-party: the upstream manifest digest and the image ID. First-party: the image
        ID alone — a local build (`install.sh --build`, ADR-0014) has no registry digest, and
        the ID is what `docker inspect` reports for the tag compose starts."""
        if not (self.image_id and _DIGEST.match(self.image_id)):
            return False
        if self.first_party:
            return True
        return bool(self.digest and _DIGEST.match(self.digest))

    def compose_ref(self) -> str:
        return f"{REGISTRY}/{self.reference}"


def third_party(name: str) -> str:
    return next(image for image in DEFAULT_IMAGES if image.name == name).compose_ref()


def started_by_compose(images: Iterable[LockedImage]) -> list[LockedImage]:
    """The images a compose file may reference as a service's `image:`."""
    return [image for image in images if image.started_by == "compose"]


def started_by_model_manager(images: Iterable[LockedImage]) -> list[LockedImage]:
    """The images the model manager starts (the vLLM image); never a compose service."""
    return [image for image in images if image.started_by == "model-manager"]


def first_party(name: str) -> str:
    return f"{REGISTRY}/slas/{name}:{VERSION}"


def _third(
    name: str,
    reference: str,
    upstream: str,
    *,
    prod_only: bool = False,
    digest: str | None = None,
    started_by: StartedBy = "compose",
) -> LockedImage:
    return LockedImage(
        name=name,
        reference=reference,
        upstream=upstream,
        digest=digest,
        profiles=["prod"] if prod_only else ["quickstart", "prod"],
        started_by=started_by,
    )


def _first(name: str, *, prod_only: bool = False) -> LockedImage:
    return LockedImage(
        name=name,
        reference=f"slas/{name}:{VERSION}",
        upstream="built offline from images/ and the vendored caches",
        first_party=True,
        signed_by="slas-release",
        profiles=["prod"] if prod_only else ["quickstart", "prod"],
    )


#: Tags are immutable upstream releases (INV-8: never `latest`). Digests and IDs are filled
#: by scripts/lock-images.sh; the marker below keeps the installer from starting until then.
DEFAULT_IMAGES: Final[tuple[LockedImage, ...]] = (
    _third("postgres", "library/postgres:16.6", "docker.io/library/postgres:16.6"),
    _third("redis", "library/redis:7.4.2", "docker.io/library/redis:7.4.2"),
    _third(
        "minio",
        "minio/minio:RELEASE.2025-04-22T22-12-26Z",
        "quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z",
    ),
    _third(
        "mc",
        "minio/mc:RELEASE.2025-04-16T18-13-26Z",
        "quay.io/minio/mc:RELEASE.2025-04-16T18-13-26Z",
        prod_only=True,
    ),
    _third("qdrant", "qdrant/qdrant:v1.13.4", "docker.io/qdrant/qdrant:v1.13.4"),
    _third("prometheus", "prom/prometheus:v3.2.1", "quay.io/prometheus/prometheus:v3.2.1"),
    _third("alertmanager", "prom/alertmanager:v0.28.1", "quay.io/prometheus/alertmanager:v0.28.1"),
    _third("grafana", "grafana/grafana:11.5.2", "docker.io/grafana/grafana:11.5.2"),
    _third(
        "dcgm-exporter",
        "nvidia/dcgm-exporter:3.3.9-3.6.1-ubuntu22.04",
        "nvcr.io/nvidia/k8s/dcgm-exporter:3.3.9-3.6.1-ubuntu22.04",
    ),
    _third("node-exporter", "prom/node-exporter:v1.9.0", "quay.io/prometheus/node-exporter:v1.9.0"),
    _third(
        "postgres-exporter",
        "prometheuscommunity/postgres-exporter:v0.17.1",
        "quay.io/prometheuscommunity/postgres-exporter:v0.17.1",
    ),
    _third("vault", "hashicorp/vault:1.18.5", "docker.io/hashicorp/vault:1.18.5", prod_only=True),
    _third(
        "keycloak", "keycloak/keycloak:26.1.4", "quay.io/keycloak/keycloak:26.1.4", prod_only=True
    ),
    _third("loki", "grafana/loki:3.4.2", "docker.io/grafana/loki:3.4.2", prod_only=True),
    _third("tempo", "grafana/tempo:2.7.1", "docker.io/grafana/tempo:2.7.1", prod_only=True),
    # Not a compose service: the model manager starts one container from it per role and per
    # voter on the inference network (CLAUDE.md §7, §12; ADR-0015). Compose passes its
    # reference to model-manager as SLAS_VLLM_IMAGE; install.sh --build pulls it by this digest.
    _third(
        "vllm",
        "vllm/vllm-openai:v0.29.0-x86_64-cu129",
        "docker.io/vllm/vllm-openai:v0.29.0-x86_64-cu129",
        digest="sha256:3e10e8189823e0f7ae4620c271bcdaaf64127ec7d0edc351591a508498b7684a",
        started_by="model-manager",
    ),
    _first("edge"),
    _first("webui"),
    _first("api"),
    _first("agent-core-orchestrator"),
    _first("llm-gateway"),
    _first("model-manager"),
    _first("sandbox-manager"),
    _first("screen-worker"),
    _first("git-broker"),
    _first("validation-executor"),
    _first("factory-executor"),
    _first("local-search-api"),
    _first("postgres-pgbackrest", prod_only=True),
)


#: The agents an installation starts (ADR-0017). `coding` is always on; the other two bring
#: their executor container and compose profile only when named in SLAS_AGENTS.
AGENTS: Final[tuple[str, ...]] = ("coding", "validation", "factory")
DEFAULT_AGENTS: Final[tuple[str, ...]] = ("coding",)
#: The first-party images only one optional agent starts.
AGENT_IMAGES: Final[dict[str, frozenset[str]]] = {
    "validation": frozenset({"validation-executor"}),
    "factory": frozenset({"factory-executor"}),
}


class AgentsError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def parse_agents(text: str | None) -> tuple[str, ...]:
    """`SLAS_AGENTS` / `--agents`: a comma list of agents; empty means the default (coding)."""
    if text is None or not text.strip():
        return DEFAULT_AGENTS
    chosen: list[str] = []
    for part in text.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in AGENTS:
            raise AgentsError(
                ThreePartMessage(
                    f'The agent "{name}" is not known.',
                    "SLAS_AGENTS or --agents names the agents to start.",
                    f"Use a comma list of {', '.join(AGENTS)}; the default is coding.",
                )
            )
        if name not in chosen:
            chosen.append(name)
    if "coding" not in chosen:
        chosen.insert(0, "coding")
    return tuple(name for name in AGENTS if name in chosen)


def images_off_for(agents: Sequence[str]) -> frozenset[str]:
    """First-party image names an installation without these agents does not start."""
    off: set[str] = set()
    for agent, names in AGENT_IMAGES.items():
        if agent not in agents:
            off |= names
    return frozenset(off)


def compose_profiles(agents: Sequence[str]) -> list[str]:
    """The compose profiles (`COMPOSE_PROFILES`) the chosen agents need."""
    return [agent for agent in AGENTS if agent in agents and agent in AGENT_IMAGES]


class ImageLock(SlasModel):
    version: int = 1
    default_registry: str = DEFAULT_REGISTRY
    images: list[LockedImage]

    def for_profile(
        self, profile: Profile, agents: Sequence[str] | None = None
    ) -> list[LockedImage]:
        """The images the profile starts; with `agents`, minus the executors of agents that
        are off (ADR-0017)."""
        off = images_off_for(agents) if agents is not None else frozenset()
        return [
            image for image in self.images if profile in image.profiles and image.name not in off
        ]

    def unpinned(self, profile: Profile, agents: Sequence[str] | None = None) -> list[LockedImage]:
        return [image for image in self.for_profile(profile, agents) if not image.pinned]

    def sentence(self, profile: Profile, agents: Sequence[str] | None = None) -> str:
        wanted = self.for_profile(profile, agents)
        missing = self.unpinned(profile, agents)
        if not missing:
            return f"All {len(wanted)} images the {profile} profile starts are pinned by digest."
        names = ", ".join(image.name for image in missing[:6]) + (
            f" and {len(missing) - 6} more" if len(missing) > 6 else ""
        )
        return (
            f"{len(wanted) - len(missing)} of {len(wanted)} images the {profile} profile starts "
            f"are pinned by digest; not pinned: {names}."
        )


def default_lock() -> ImageLock:
    return ImageLock(images=list(DEFAULT_IMAGES))


LOCK_HEADER: Final = (
    "Image lock for SW Local Agent Service (ADR-0003, INV-8). Rendered from\n"
    "slas_deploy.images.DEFAULT_IMAGES; a unit test keeps file and code in step; the JSON twin\n"
    "is what install.sh reads. Tags are immutable upstream releases. `digest` and `image_id`\n"
    "are filled by scripts/lock-images.sh on a connected build host and signed with cosign;\n"
    "while any image the chosen profile starts is null, install.sh refuses to start it.\n"
    "`started_by: model-manager` marks the vLLM image: pulled, saved and locked like the\n"
    "others, never a compose service — the model manager starts it per role and voter."
)


def render_lock_yaml(lock: ImageLock, *, header: str = LOCK_HEADER) -> str:
    lines = [f"# {line}".rstrip() for line in header.splitlines()]
    lines += [f"version: {lock.version}", f"default_registry: {lock.default_registry}", "images:"]
    for image in lock.images:
        lines.append(f"  - name: {image.name}")
        lines.append(f"    reference: {json.dumps(image.reference)}")
        lines.append(f"    upstream: {json.dumps(image.upstream)}")
        lines.append(f"    first_party: {'true' if image.first_party else 'false'}")
        lines.append(f"    digest: {json.dumps(image.digest)}")
        lines.append(f"    image_id: {json.dumps(image.image_id)}")
        lines.append(f"    signed_by: {json.dumps(image.signed_by)}")
        lines.append(f"    profiles: [{', '.join(image.profiles)}]")
        lines.append(f"    started_by: {image.started_by}")
    return "\n".join(lines) + "\n"


def render_lock_json(lock: ImageLock) -> str:
    return json.dumps(lock.model_dump(mode="json"), indent=2) + "\n"


def parse_lock_json(text: str) -> ImageLock:
    return ImageLock.model_validate_json(text)


# --- what install.sh checks -----------------------------------------------------------------------


class BundleManifest(SlasModel):
    """`manifest.json` in a bundle: which image IDs the saved tarballs carry, signed by cosign."""

    version: str = Field(min_length=1)
    built_at: str = Field(min_length=1)
    images: dict[str, str] = Field(default_factory=dict)  # compose ref → image ID
    files: dict[str, str] = Field(default_factory=dict)  # relative path → sha256


class LockError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def check_lock(
    lock: ImageLock, profile: Profile, agents: Sequence[str] | None = None
) -> list[LockedImage]:
    """The images the profile starts, or a three-part error when one is not pinned or is
    referenced by a mutable tag."""
    wanted = lock.for_profile(profile, agents)
    for image in wanted:
        if _LATEST.search(image.reference):
            raise LockError(
                ThreePartMessage(
                    f"The image {image.reference} is not pinned to a release.",
                    "Its reference has no tag or uses `latest` (INV-8).",
                    "Pin it to an immutable tag in slas_deploy.images and render the lock again.",
                )
            )
    missing = lock.unpinned(profile, agents)
    if missing:
        raise LockError(
            ThreePartMessage(
                lock.sentence(profile, agents),
                "The lock has no digest or image ID for them yet; nothing has verified what "
                "those tags point at.",
                "On a connected build host run scripts/lock-images.sh, commit the lock and the "
                "bundle it produces, then install from that bundle; or, on a connected "
                "quickstart host, run ./install.sh --build (ADR-0014).",
            )
        )
    return wanted


def check_manifest(
    lock: ImageLock, manifest: BundleManifest, profile: Profile, *, registry: str = DEFAULT_REGISTRY
) -> list[str]:
    """Every image the profile starts must be in the bundle with the ID the lock records."""
    problems: list[str] = []
    for image in check_lock(lock, profile):
        ref = f"{registry}/{image.reference}".replace(VERSION, manifest.version)
        found = manifest.images.get(ref)
        if found is None:
            problems.append(f"{ref} is not in the bundle")
        elif not image.first_party and found != image.image_id:
            problems.append(
                f"{ref} has image ID {found[:19]}…, the lock expects {str(image.image_id)[:19]}…"
            )
    return problems


def registry_for(profile: Profile, environ: Mapping[str, str]) -> str:
    """Quickstart loads a bundle tagged for `registry.internal`; prod pulls from Harbor."""
    configured = environ.get("SLAS_REGISTRY", "").strip()
    if configured:
        return configured
    return PROD_REGISTRY_HINT if profile == "prod" else DEFAULT_REGISTRY


def names(images: Iterable[LockedImage]) -> list[str]:
    return [image.name for image in images]


def as_table(lock: ImageLock, profile: Profile) -> list[dict[str, Any]]:
    return [
        {
            "name": image.name,
            "reference": image.reference,
            "pinned": image.pinned,
            "first_party": image.first_party,
        }
        for image in lock.for_profile(profile)
    ]
