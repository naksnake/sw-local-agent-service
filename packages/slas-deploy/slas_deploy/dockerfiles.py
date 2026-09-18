"""The Dockerfiles of the first-party Python service images (ADR-0003, ADR-0014, INV-8),
rendered from one template so nine images cannot drift apart.

    images/<name>/Dockerfile        one per Python service; `docker build -f … .` from the
                                    repository root, which is the context for every
                                    first-party image
    images/edge, images/webui,      hand-written (Caddy, a Node build stage, Xvfb); the
    images/screen-worker            same base pins, listed in BASES so a test can check them

Every base is pinned by digest; the human-readable tag sits next to it for the reader and is
what `docker manifest inspect` was asked for. Python dependencies come from `uv.lock`
(`uv sync --frozen`), so a build resolves nothing. Each service's CMD is its console script's
`serve` (docs/api-contract-round-2.md §1, ADR-0015), which builds the `slas_http` app and
serves `/health` and `/metrics` on 8000 — what the compose healthcheck probes and Prometheus
scrapes. A service whose entrypoint has not landed (local-search-api) keeps the placeholder
`python -m slas_observability.serve <name>`, which answers the same two routes.
"""

from __future__ import annotations

# ruff: noqa: E501 — digests and Dockerfile lines read better unwrapped
from dataclasses import dataclass
from typing import Final

#: Base images by digest, with the tag each digest was resolved from. A test checks that
#: every FROM line in images/ uses one of these digests (or a stage name).
BASES: Final[dict[str, tuple[str, str]]] = {
    # name: (tag for humans, reference by digest for FROM)
    "python": (
        "python:3.12.14-slim-bookworm",
        "docker.io/library/python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254",
    ),
    "uv": (
        "ghcr.io/astral-sh/uv:0.8.17",
        "ghcr.io/astral-sh/uv@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1",
    ),
    "debian": (
        "debian:bookworm-20250908-slim",
        "docker.io/library/debian@sha256:df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3",
    ),
    "caddy": (
        "caddy:2.11.4",
        "docker.io/library/caddy@sha256:13ba145cba2f3e28fa801994876e4c086d1b95d5aa2a520a734765ffb6b12017",
    ),
    "node": (
        "node:22.22.2-bookworm-slim",
        "docker.io/library/node@sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e",
    ),
}

#: The one uid every first-party image runs as unless compose sets `user:`.
IMAGE_UID: Final = 10001
HEALTH_SOURCE: Final = "images/slas-health/slas-health"
HEALTH_TARGET: Final = "/usr/local/bin/slas-health"
VENV: Final = "/opt/slas/.venv"


@dataclass(frozen=True)
class PythonService:
    #: The image and compose service name.
    name: str
    #: The uv workspace member `uv sync --package` installs (with its dependency closure).
    package: str
    #: What the image does, for the header line.
    note: str
    #: Debian packages the service needs beside Python.
    apt: tuple[str, ...] = ()
    #: The container command; None means the placeholder health runner.
    command: tuple[str, ...] | None = None

    @property
    def cmd(self) -> tuple[str, ...]:
        if self.command is not None:
            return self.command
        return ("python", "-m", "slas_observability.serve", self.name)


PYTHON_SERVICES: Final[tuple[PythonService, ...]] = (
    PythonService(
        "api",
        "slas-api",
        "the API: authz, people, settings, tickets (apps/api; docs/api-contract.md)",
        command=("slas-api", "serve"),
    ),
    PythonService(
        "agent-core-orchestrator",
        "slas-orchestrator",
        "the orchestrator running the Agent Kernel (CLAUDE.md §5.1)",
        command=("slas-orchestrator", "serve"),
    ),
    PythonService(
        "llm-gateway",
        "slas-llm-gateway",
        "the LLM gateway with the Consensus Router (CLAUDE.md §5.3)",
        command=("slas-gateway", "serve"),
    ),
    PythonService(
        "model-manager",
        "slas-model-manager",
        "the model manager: registry, fit, blue/green swaps, the vllm-* containers (CLAUDE.md §7)",
        command=("slas-model-manager", "serve"),
    ),
    PythonService(
        "sandbox-manager",
        "slas-sandbox-manager",
        "the sandbox manager for Zone A (CLAUDE.md §4.1)",
        command=("slas-sandbox-manager", "serve"),
    ),
    PythonService(
        "git-broker",
        "slas-git-broker",
        "the Git broker, the only holder of Git credentials (CLAUDE.md §5.7, INV-14)",
        apt=("git", "openssh-client", "ca-certificates"),
        command=("slas-git-broker", "serve"),
    ),
    PythonService(
        "validation-executor",
        "slas-validation-executor",
        "the validation executor, Zone B (CLAUDE.md §10.2)",
        apt=("openssh-client", "ipmitool"),
        command=("slas-validation-executor", "serve"),
    ),
    PythonService(
        "factory-executor",
        "slas-factory-executor",
        "the factory executor, Zone B' (CLAUDE.md §10.3); openssl drives the station CA",
        apt=("openssh-client", "openssl"),
        command=("slas-factory-executor", "serve"),
    ),
    PythonService(
        "local-search-api",
        "slas-observability",
        "internal-corpus search (CLAUDE.md §8.3 Option A); health only until its round",
    ),
)

#: First-party images whose Dockerfile is written by hand under images/<name>/.
HAND_WRITTEN: Final[tuple[str, ...]] = ("edge", "webui", "screen-worker", "postgres-pgbackrest")


def dockerfile_path(name: str) -> str:
    return f"images/{name}/Dockerfile"


def python_service_dockerfile(service: PythonService) -> str:
    python_tag, python_ref = BASES["python"]
    uv_tag, uv_ref = BASES["uv"]
    cmd = ", ".join(f'"{part}"' for part in service.cmd)
    lines = [
        f"# {service.name} image: {service.note}.",
        "# Rendered from slas_deploy.dockerfiles; a unit test keeps this file in step. Build from the",
        f"# repository root:  docker build -f {dockerfile_path(service.name)} -t <registry>/slas/{service.name}:<version> .",
        f"# Bases pinned by digest (INV-8): {python_tag}; the static uv binary from {uv_tag}.",
        "# Python dependencies come from uv.lock (--frozen): the build resolves nothing.",
        "",
        f"FROM {uv_ref} AS uv",
        f"FROM {python_ref} AS build",
        "COPY --from=uv /uv /usr/local/bin/uv",
        "# No bytecode compile in the build: uv's compile step opens every file in the venv at",
        '# once and fails with "No file descriptors available" under the 1024-descriptor limit',
        "# Docker 25+ gives a build step. Python compiles on first import instead (a second).",
        f"ENV UV_PROJECT_ENVIRONMENT={VENV} UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \\",
        "    UV_COMPILE_BYTECODE=0 UV_NO_PROGRESS=1 UV_CACHE_DIR=/tmp/uv-cache",
        "WORKDIR /opt/slas",
        "# Resolving one workspace member needs every member's pyproject: packages/, services/ and",
        "# apps/api. The lock pins the versions; --no-editable makes the venv self-contained.",
        "COPY pyproject.toml uv.lock ./",
        "COPY packages/ packages/",
        "COPY services/ services/",
        "COPY apps/api/ apps/api/",
        f"RUN uv sync --frozen --no-dev --no-editable --package {service.package} \\",
        " && rm -rf /tmp/uv-cache",
        "",
        f"FROM {python_ref}",
    ]
    if service.apt:
        lines += [
            "# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle build",
            "# (INV-8); the offline bundle path builds from a vendored apt cache.",
            "RUN apt-get update \\",
            " && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
            + " ".join(service.apt)
            + " \\",
            " && rm -rf /var/lib/apt/lists/*",
        ]
    lines += [
        f"COPY --from=build {VENV} {VENV}",
        f"COPY {HEALTH_SOURCE} {HEALTH_TARGET}",
        f"RUN chmod 0755 {HEALTH_TARGET} \\",
        f" && groupadd --gid {IMAGE_UID} slas \\",
        f" && useradd --uid {IMAGE_UID} --gid {IMAGE_UID} --no-create-home --shell /usr/sbin/nologin slas \\",
        f" && mkdir -p /data && chown {IMAGE_UID}:{IMAGE_UID} /data",
        "# compose may run the container as another uid (${SLAS_UID}:${SLAS_GID}); nothing here",
        "# needs a home directory or a writable image layer.",
        f'ENV PATH="{VENV}/bin:${{PATH}}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp',
        f"USER {IMAGE_UID}:{IMAGE_UID}",
        "WORKDIR /opt/slas",
        "EXPOSE 8000",
        f"CMD [{cmd}]",
    ]
    return "\n".join(lines) + "\n"


def rendered_dockerfiles() -> dict[str, str]:
    return {
        dockerfile_path(service.name): python_service_dockerfile(service)
        for service in PYTHON_SERVICES
    }
