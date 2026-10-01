# Sandbox image for Rust (rustc 1.80.1) — CLAUDE.md §4.1 Zone A.
# Rendered from slas_sandbox_manager.images; a unit test keeps this file in step. Build
# from the repository root:  docker build -f images/sandbox-rust/Dockerfile -t local/slas/sandbox-rust:1.80.1 .
# Connected build (ADR-0014): the toolchain comes from the pinned upstream image and the
# pinned installs below; the offline bundle variant is rendered with --source bundle.
# Base pinned by digest (INV-8): rust:1.80.1-slim-bookworm
FROM docker.io/library/rust@sha256:907ff4b3ee7df57149ffee04f606e0a08b9b2ed3507f00a19cf3c9c0f74b7681

# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle
# build (INV-8); on the connected path this reaches the Debian mirror (ADR-0014).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git make pkg-config \
 && rm -rf /var/lib/apt/lists/*

RUN rustup component add clippy rustfmt
ENV SLAS_LANGUAGE=rust SLAS_TOOLCHAIN_VERSION=1.80.1 CARGO_HOME=/scratch/.cargo CARGO_NET_OFFLINE=true
# The image provides exactly the version the toolchain manifest lists; the build fails
# otherwise, so the images and Toolchains/manifest.json cannot drift apart.
RUN rustc --version 2>&1 | grep -F "1.80.1"

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
