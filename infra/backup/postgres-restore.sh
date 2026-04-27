#!/usr/bin/env bash
# postgres-restore.sh — download, age-decrypt, restore into Postgres.
# Usage: ./postgres-restore.sh <s3-key> [--dry-run]
# Required: POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, BACKUP_S3_BUCKET
# Optional: AGE_IDENTITY_FILE (default ~/.config/age/key.txt), BACKUP_S3_ENDPOINT

set -euo pipefail

S3_KEY="${1:?Usage: $0 <s3-key> [--dry-run]}"
DRY_RUN=false; [[ "${2:-}" == "--dry-run" ]] && DRY_RUN=true

: "${POSTGRES_USER:?required}" "${POSTGRES_PASSWORD:?required}"
: "${POSTGRES_DB:?required}"   "${BACKUP_S3_BUCKET:?required}"

AGE_IDENTITY_FILE="${AGE_IDENTITY_FILE:-${HOME}/.config/age/key.txt}"
[[ -f "${AGE_IDENTITY_FILE}" ]] || {
    echo "[restore] ERROR: age identity file not found: ${AGE_IDENTITY_FILE}" >&2
    echo "[restore] Generate with: age-keygen -o ~/.config/age/key.txt" >&2
    exit 1
}

S3_EXTRA_FLAGS=()
[[ -n "${BACKUP_S3_ENDPOINT:-}" ]] && S3_EXTRA_FLAGS+=(--endpoint-url "${BACKUP_S3_ENDPOINT}")

echo "[restore] Source: s3://${BACKUP_S3_BUCKET}/${S3_KEY}"
echo "[restore] Target: ${POSTGRES_DB}@postgres as ${POSTGRES_USER}"

if [[ "${DRY_RUN}" == "true" ]]; then
    aws s3api head-object --bucket "${BACKUP_S3_BUCKET}" --key "${S3_KEY}" "${S3_EXTRA_FLAGS[@]}"
    echo "[restore] DRY RUN — object exists. No changes made."
    exit 0
fi

echo ""; echo "WARNING: This will DROP and recreate '${POSTGRES_DB}'. All data deleted."
read -r -p "Type YES to continue: " CONFIRM
[[ "${CONFIRM}" == "YES" ]] || { echo "[restore] Aborted."; exit 1; }

echo "[restore] Downloading, decrypting, and restoring..."
PGPASSWORD="${POSTGRES_PASSWORD}" \
aws s3 cp "s3://${BACKUP_S3_BUCKET}/${S3_KEY}" - "${S3_EXTRA_FLAGS[@]}" \
  | age --decrypt --identity "${AGE_IDENTITY_FILE}" \
  | pg_restore \
        --host=postgres --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        --clean --if-exists --no-owner --no-privileges --verbose

echo "[restore] Restore complete. Verifying key tables..."
PGPASSWORD="${POSTGRES_PASSWORD}" \
psql --host=postgres --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" --no-psqlrc \
    -c "SELECT 'api_keys' AS tbl, count(*) FROM api_keys
        UNION ALL SELECT 'jobs', count(*) FROM jobs
        UNION ALL SELECT 'audit_log', count(*) FROM audit_log;"

echo "[restore] Done."
