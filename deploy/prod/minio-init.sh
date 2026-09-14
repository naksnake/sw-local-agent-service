#!/bin/sh
# MinIO buckets for the prod profile (CLAUDE.md §8.4 object-lock, ADR-0012). Rendered
# from slas_deploy.pgbackrest; a unit test keeps file and code in step. Runs once per
# `docker compose up` as the minio-init service; every step is idempotent.
set -eu
ROOT_PASSWORD="$(cat /run/secrets/minio_root_password)"
BACKUP_KEY="$(cat /run/secrets/pgbackrest_s3_key)"
BACKUP_SECRET="$(cat /run/secrets/pgbackrest_s3_secret)"
mc alias set slas http://minio:9000 "$MINIO_ROOT_USER" "$ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing --with-lock slas/slas-backups
mc mb --ignore-existing --with-lock slas/slas-artifacts
mc retention set --default compliance "${BACKUP_RETENTION_DAYS}d" slas/slas-backups
mc retention set --default governance "${ARTIFACT_RETENTION_DAYS}d" slas/slas-artifacts
mc version enable slas/slas-backups
mc version enable slas/slas-artifacts
# The backup user may write and read the backup bucket and nothing else.
mc admin user add slas "$BACKUP_KEY" "$BACKUP_SECRET" >/dev/null 2>&1 || true
cat > /tmp/backup-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": ["arn:aws:s3:::slas-backups"]},
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectRetention"],
     "Resource": ["arn:aws:s3:::slas-backups/*"]}
  ]
}
EOF
mc admin policy create slas slas-backup-writer /tmp/backup-policy.json >/dev/null 2>&1 || true
mc admin policy attach slas slas-backup-writer --user "$BACKUP_KEY" >/dev/null 2>&1 || true
echo "Object lock is on: slas-backups keeps backups ${BACKUP_RETENTION_DAYS} days (compliance), slas-artifacts keeps run artifacts ${ARTIFACT_RETENTION_DAYS} days (governance)."
