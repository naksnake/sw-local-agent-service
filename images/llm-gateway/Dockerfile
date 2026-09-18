# llm-gateway image: the LLM gateway with the Consensus Router (CLAUDE.md §5.3).
# Rendered from slas_deploy.dockerfiles; a unit test keeps this file in step. Build from the
# repository root:  docker build -f images/llm-gateway/Dockerfile -t <registry>/slas/llm-gateway:<version> .
# Bases pinned by digest (INV-8): python:3.12.14-slim-bookworm; the static uv binary from ghcr.io/astral-sh/uv:0.8.17.
# Python dependencies come from uv.lock (--frozen): the build resolves nothing.

FROM ghcr.io/astral-sh/uv@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 AS uv
FROM docker.io/library/python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
COPY --from=uv /uv /usr/local/bin/uv
# No bytecode compile in the build: uv's compile step opens every file in the venv at
# once and fails with "No file descriptors available" under the 1024-descriptor limit
# Docker 25+ gives a build step. Python compiles on first import instead (a second).
ENV UV_PROJECT_ENVIRONMENT=/opt/slas/.venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0 UV_NO_PROGRESS=1 UV_CACHE_DIR=/tmp/uv-cache
WORKDIR /opt/slas
# Resolving one workspace member needs every member's pyproject: packages/, services/ and
# apps/api. The lock pins the versions; --no-editable makes the venv self-contained.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY services/ services/
COPY apps/api/ apps/api/
RUN uv sync --frozen --no-dev --no-editable --package slas-llm-gateway \
 && rm -rf /tmp/uv-cache

FROM docker.io/library/python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
COPY --from=build /opt/slas/.venv /opt/slas/.venv
COPY images/slas-health/slas-health /usr/local/bin/slas-health
RUN chmod 0755 /usr/local/bin/slas-health \
 && groupadd --gid 10001 slas \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin slas \
 && mkdir -p /data && chown 10001:10001 /data
# compose may run the container as another uid (${SLAS_UID}:${SLAS_GID}); nothing here
# needs a home directory or a writable image layer.
ENV PATH="/opt/slas/.venv/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
WORKDIR /opt/slas
EXPOSE 8000
CMD ["slas-gateway", "serve"]
