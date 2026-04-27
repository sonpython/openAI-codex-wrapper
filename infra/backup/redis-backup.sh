#!/usr/bin/env bash
# redis-backup.sh — trigger BGSAVE, age-encrypt RDB, upload to S3/B2/R2.
# Runs as daily cron inside the postgres-backup container after postgres-backup.sh.
#
# Required env vars:
#   BACKUP_AGE_RECIPIENT  age public key
#   BACKUP_S3_BUCKET      S3/B2/R2 bucket name
#
# Optional env vars:
#   REDIS_HOST            default: redis
#   REDIS_PORT            default: 6379
#   BACKUP_S3_ENDPOINT    Override endpoint URL for B2/R2

set -euo pipefail

: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is required}"
: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET is required}"

REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_NAME="redis-${TS}.rdb.age"
S3_KEY="redis/${BACKUP_NAME}"

S3_EXTRA_FLAGS=()
if [[ -n "${BACKUP_S3_ENDPOINT:-}" ]]; then
    S3_EXTRA_FLAGS+=(--endpoint-url "${BACKUP_S3_ENDPOINT}")
fi

echo "[redis-backup] Triggering BGSAVE on ${REDIS_HOST}:${REDIS_PORT}..."
redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" BGSAVE

# Wait for background save to complete (poll LASTSAVE timestamp).
BEFORE=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" LASTSAVE)
DEADLINE=$(( $(date +%s) + 120 ))
while true; do
    CURRENT=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" LASTSAVE)
    if [[ "${CURRENT}" -gt "${BEFORE}" ]]; then
        break
    fi
    if [[ $(date +%s) -gt "${DEADLINE}" ]]; then
        echo "[redis-backup] ERROR: BGSAVE did not complete within 120s" >&2
        exit 1
    fi
    sleep 2
done

echo "[redis-backup] BGSAVE complete. Uploading ${BACKUP_NAME}..."

# Stream RDB file through age encryption directly to S3 (no temp file on disk).
cat /data/dump.rdb \
  | age --recipient "${BACKUP_AGE_RECIPIENT}" \
  | aws s3 cp - "s3://${BACKUP_S3_BUCKET}/${S3_KEY}" \
      "${S3_EXTRA_FLAGS[@]}" \
      --no-progress

echo "[redis-backup] Upload complete: s3://${BACKUP_S3_BUCKET}/${S3_KEY}"
