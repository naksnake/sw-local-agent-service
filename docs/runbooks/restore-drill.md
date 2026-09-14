# Restore drill: proving the backups bring the platform back, and how long it takes

The prod profile archives PostgreSQL continuously with pgBackRest to a MinIO repository
under object lock (CLAUDE.md §8.4, ADR-0012). A backup nobody has restored is a hope, not a
backup: this drill restores to a point in time on a schedule, measures the recovery time
(RTO), and records it. Quarterly at least; after every upgrade of PostgreSQL or pgBackRest.

## 0 · What you need

| | |
|---|---|
| Where | The reference host for the first drill; a staging host afterwards. Never the production host while a run is in progress. |
| Who | An administrator with Docker access on the platform host (equivalent to administrator, ADR-0007). |
| Time | Block two hours the first time. |
| Before | `slas status` is clean; `slas backup status` lists at least one full backup and the archive check passes. |

## 1 · Pick the point in time

```
slas backup status
```

lists the backups in the locked repository and confirms the archive is continuous. Choose
a moment after the latest full backup and before a known write, for example a ticket you
created ten minutes ago: after the drill that ticket must be gone and everything before it
present.

## 2 · Run the drill

```
slas backup drill --to 2026-09-14T02:30:00+00:00 --environment "reference host"
```

The drill, phase by phase, each timed:

| Phase | What happens | Why |
|---|---|---|
| stop writers | `docker compose stop api agent-core-orchestrator git-broker validation-executor factory-executor` | No process writes to the database during the restore. |
| stop postgres | `docker compose stop postgres` | pgBackRest restores into the data directory. |
| pgbackrest restore | `pgbackrest --stanza=slas restore --delta --type=time --target=… --target-action=promote` in `backup-runner`, which shares the data volume | Replaces the cluster with the chosen point; `--delta` copies only what differs. |
| start postgres | `docker compose start postgres` | PostgreSQL replays WAL from the archive up to the target and promotes. |
| wait until ready | `pg_isready` inside the container, up to 15 minutes | Recovery time depends on how much WAL follows the last backup. |
| verify the platform's data | `slas-api verify-restore` (arrives with apps/api: counts tickets, users and settings and compares with the pre-drill numbers) | A database that starts is not yet a platform that is back. |
| start writers | the services stopped first | The platform is back. |

The record lands under `${SLAS_DATA_ROOT}/Backups/drills/<timestamp>.json` and the
command prints one sentence:

> Restore drill on 2026-09-14 (reference host): the platform came back to 2026-09-14 02:30 in 11.5 minutes across 7 phases.

Object lock means the drill cannot damage the repository: a restore only reads it.

## 3 · Check by hand

1. Sign in. The ticket you created after the target time is gone; older tickets are there.
2. Open a ticket's journal: entries before the target are intact, the trace ids match.
3. `slas status` is clean; the Factory and Validation lease files still name what was held
   at the target time — release stale leases by hand if a run was in flight.
4. Qdrant: `Backups/qdrant/` holds the snapshot taken with the backup; restore it if the
   knowledge index is older than the database (`tar -C ${SLAS_DATA_ROOT} -xzf …`, then
   restart vector-db). The drill records this as a manual step until the snapshot restore
   is automated.

## 4 · The RTO table

Every drill adds a row. The target the platform commits to is **30 minutes** to a working
sign-in page with the database at the chosen point (CLAUDE.md §3 "restore drills").

| Date | Environment | Target time | Data size | RTO | Result | Notes |
|---|---|---|---|---|---|---|
| 2026-09-14 | rehearsal against fakes (`tests/unit/test_oidc_kata_backup.py`) | 02:30 | none | 4.0 min (simulated: 30 s per phase) | came back | Proves the phases, the ordering and the record; **not a measurement**. |
| *not yet run* | reference host | | | | | The first real measurement is a release-checklist item: this environment has no Docker, no Postgres and no MinIO. Run `slas backup drill` on the reference host and add the row. |

A drill that does not come back is a release blocker: open a ticket with the drill record
attached and do not upgrade until the next drill passes.

## 5 · When it is not a drill

A real restore is the same command without `drill`:

```
slas backup restore --to <time> --yes
```

Stop the writers yourself first (the `drill` does it for you; `restore` assumes you did),
then start them after `pg_isready`. If the whole host is lost: install the same release on
a new host with `./install.sh --profile prod`, point `config/pgbackrest.conf`'s repository
at the surviving MinIO (or restore MinIO's data directory from its own mirror first), then
`slas backup restore --yes`. The object-locked repository is the copy that survives an
operator's mistake; the MinIO mirror on a second host is the copy that survives the host.
