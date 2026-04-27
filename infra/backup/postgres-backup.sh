#!/usr/bin/env bash
# postgres-backup.sh — dump, age-encrypt, upload to S3/B2/R2.
# Runs as daily cron inside the postgres-backup container (02:00 UTC).
#
# Required env vars:
#   POSTGRES_USER         DB username
#   POSTGRES_PASSWORD     DB password
#   POSTGRES_DB           DB name
#   BACKUP_AGE_RECIPIENT  age public key (e.g. age1ql3z7hjy...)
#   BACKUP_S3_BUCKET      S3/B2/R2 bucket name
#
# Optional env vars:
#   BACKUP_S3_ENDPOINT    Override endpoint URL for B2/R2 (default: AWS S3)
#   AWS_ACCESS_KEY_ID     S3 credentials (can also come from instance profile)
#   AWS_SECRET_ACCESS_KEY

set -euo pipefail

# ── Validation ────────────────────────────────────────────────────────────────
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is required}"
: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required}"

# ── Timestamp + naming ────────────────────────────────────────────────────────
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_NAME="${POSTGRES_DB}-${TS}.dump.age"
S3_KEY="postgres/${BACKUP_NAME}"

echo "[backup] Starting postgres backup: ${BACKUP_NAME}"

# ── Endpoint flag (optional for B2 / R2 / MinIO) ─────────────────────────────
S3_EXTRA_FLAGS=()
if [[ -n "${BACKUP_S3_ENDPOINT:-}" ]]; then
    S3_EXTRA_FLAGS+=(--endpoint-url "${BACKUP_S3_ENDPOINT}")
fi

# ── Pipeline: pg_dump | age encrypt | aws s3 cp ───────────────────────────────
# Uses process substitution to avoid writing plaintext to disk.
PGPASSWORD="${POSTGRES_PASSWORD}" \
pg_dump \
    --host=postgres \
    --username="${POSTGRES_USER}" \
    --dbname="${POSTGRES_DB}" \
    --format=custom \
    --no-owner \
    --no-privileges \
  | age --recipient "${BACKUP_AGE_RECIPIENT}" \
  | aws s3 cp - "s3://${BACKUP_S3_BUCKET}/${S3_KEY}" \
      "${S3_EXTRA_FLAGS[@]}" \
      --expected-size 0 \
      --no-progress

echo "[backup] Upload complete: s3://${BACKUP_S3_BUCKET}/${S3_KEY}"

# ── Verify upload ─────────────────────────────────────────────────────────────
UPLOADED_SIZE=$(
    aws s3api head-object \
        --bucket "${BACKUP_S3_BUCKET}" \
        --key "${S3_KEY}" \
        "${S3_EXTRA_FLAGS[@]}" \
        --query ContentLength \
        --output text
)

if [[ "${UPLOADED_SIZE}" -lt 100 ]]; then
    echo "[backup] ERROR: uploaded object suspiciously small (${UPLOADED_SIZE} bytes)" >&2
    exit 1
fi

echo "[backup] Verified: ${UPLOADED_SIZE} bytes at s3://${BACKUP_S3_BUCKET}/${S3_KEY}"
echo "[backup] Done: ${BACKUP_NAME}"

# ── Retention: managed via S3 lifecycle policy (30 days) ─────────────────────
# Configure on the bucket, not here:
#   aws s3api put-bucket-lifecycle-configuration \
#     --bucket "$BUCKET" \
#     --lifecycle-configuration file://lifecycle-30d.json
# lifecycle-30d.json: { Rules: [{ Prefix: "postgres/", Expiration: { Days: 30 } }] }
