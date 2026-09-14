# deploy/prod

Scripts the prod profile's containers run, rendered from `slas_deploy` (ADR-0012); a unit
test keeps them in step.

| File | Runs in | What it does |
|---|---|---|
| `minio-init.sh` | `minio-init` (once per `compose up`) | Creates `slas-backups` and `slas-artifacts` with object lock — compliance retention for backups, governance for artifacts — enables versioning, and gives the backup user write access to the backup bucket only. |
| `backup-runner.sh` | `backup-runner` | The schedule: a full backup on the configured weekday, a differential on the other days at the configured hour, an archive check every hour, a Qdrant snapshot next to every backup; one JSON line per run in `Backups/backup-journal.jsonl`. |
| `vault-bootstrap.sh` | `vault` (once, by `install.sh`) | Enables KV v2 at `slas/` and AppRole, writes one policy per service and creates the roles. |

`../station-runner/` is the offline bundle for physical test stations (P10).
