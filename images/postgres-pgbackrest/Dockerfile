# PostgreSQL 16 with pgBackRest for the prod profile (CLAUDE.md §8.4, ADR-0012).
# Used by the `postgres` service (archive_command needs the binary next to the server) and by
# `backup-runner` (the schedule, `slas backup`). Base pinned by immutable tag; its digest and
# image ID live in compose/images.lock.* (INV-8). Built offline from the vendored apt cache,
# from the repository root:  docker build -f images/postgres-pgbackrest/Dockerfile .
ARG SLAS_REGISTRY=registry.internal
FROM ${SLAS_REGISTRY}/library/postgres:16.6

# TODO(SLAS-IMAGES): pin the pgbackrest package version from the snapshot mirror in the
# bundle build (INV-8); the platform builds images offline from a vendored apt cache.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pgbackrest \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/spool/pgbackrest /var/log/pgbackrest \
 && chown -R postgres:postgres /var/spool/pgbackrest /var/log/pgbackrest
COPY images/slas-health/slas-health /usr/local/bin/slas-health
RUN chmod 0755 /usr/local/bin/slas-health
USER 999:999
