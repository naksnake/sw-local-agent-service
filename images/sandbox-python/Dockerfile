# Sandbox image for Python (python 3.12.6) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step. Build
# from the repository root:  docker build -f images/sandbox-python/Dockerfile -t local/slas/sandbox-python:3.12.6 .
# Connected build (ADR-0014): the toolchain comes from the pinned upstream image and the
# pinned installs below; the offline bundle variant is rendered with --source bundle.
# Base pinned by digest (INV-8): python:3.12.6-slim-bookworm
FROM docker.io/library/python@sha256:ad48727987b259854d52241fac3bc633574364867b8e20aec305e6e7f4028b26

# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle
# build (INV-8); on the connected path this reaches the Debian mirror (ADR-0014).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git make \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ruff==0.16.7 mypy==2.3.1 pytest==9.1.1
ENV SLAS_LANGUAGE=python SLAS_TOOLCHAIN_VERSION=3.12.6 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INDEX=1
# The image provides exactly the version the toolchain manifest lists; the build fails
# otherwise, so the images and Toolchains/manifest.json cannot drift apart.
RUN python3 --version 2>&1 | grep -F "3.12.6"

COPY images/sandbox-common/slas-check.sh /usr/local/bin/slas-check
RUN chmod 0755 /usr/local/bin/slas-check \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin workspace \
 && mkdir -p /workspace /scratch /etc/slas && chown 10001:10001 /workspace /scratch

# No credential helper, no remote, no hooks: the sandbox commits locally and nothing else
# (INV-14). The user's identity arrives read-only at /etc/slas/gitconfig at run time.
ENV GIT_CONFIG_GLOBAL=/etc/slas/gitconfig GIT_TERMINAL_PROMPT=0 HOME=/scratch

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT []
CMD ["sleep", "infinity"]
