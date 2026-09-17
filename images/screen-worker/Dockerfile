# screen-worker image: Xvfb + x11vnc + noVNC + xdotool + ImageMagick (CLAUDE.md §5.2, ADR-0002).
# Runs one virtual display per session; never touches the host display (INV-4). Build from
# the repository root:  docker build -f images/screen-worker/Dockerfile -t <registry>/slas/screen-worker:<version> .
# Base pinned by digest (INV-8): debian:bookworm-20250908-slim
FROM docker.io/library/debian@sha256:df52e55e3361a81ac1bead266f3373ee55d29aa50cf0975d440c2be3483d8ed3

# TODO(SLAS-IMAGES): pin apt package versions from a snapshot mirror in the bundle build
# (INV-8); the platform builds images offline from a vendored apt cache in Phase 4's CI job.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify xdotool imagemagick python3 x11-utils \
 && rm -rf /var/lib/apt/lists/*

# /health and /metrics on 8000 (compose healthcheck, Prometheus): the stdlib-only
# slas_observability package, run beside the display by the entrypoint.
COPY packages/slas-observability/slas_observability /opt/slas/slas_observability
COPY images/slas-health/slas-health /usr/local/bin/slas-health
COPY services/screen-worker/entrypoint.sh /usr/local/bin/slas-screen-worker
RUN chmod 0755 /usr/local/bin/slas-screen-worker /usr/local/bin/slas-health \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin screen

USER 10001:10001
ENV DISPLAYS_PER_WORKER=8 ACTION_RATE_LIMIT=10 PYTHONPATH=/opt/slas PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/slas-screen-worker"]
