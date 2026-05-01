#!/usr/bin/env bash
# rotate-admin-token.sh — Generate a new ADMIN_TOKEN, update .env, restart gateway.
#
# Usage:
#   bash scripts/rotate-admin-token.sh            # rotate + restart
#   bash scripts/rotate-admin-token.sh --dry-run  # print new token only, no changes
#
# Portability:
#   Uses `sed -i.bak` (macOS-compatible form; GNU sed ignores the .bak suffix).
#   Requires: openssl, sed, docker compose (or docker-compose).

set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

ENV_FILE="${ENV_FILE:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Run from the project root." >&2
  exit 1
fi

NEW_TOKEN="$(openssl rand -hex 32)"

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry run — new token would be: $NEW_TOKEN"
  echo "No files modified, no services restarted."
  exit 0
fi

# Portable in-place sed: -i.bak works on both macOS (BSD sed) and GNU sed.
# The .bak file is created as a safety net — delete after verifying.
sed -i.bak "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=${NEW_TOKEN}/" "$ENV_FILE"

echo "New ADMIN_TOKEN written to $ENV_FILE"
echo "Old token backed up in ${ENV_FILE}.bak — delete after verifying the new token works."
echo ""
echo "New ADMIN_TOKEN: ${NEW_TOKEN}"
echo ""

# Restart only the gateway container to pick up the new token without
# disrupting the worker, postgres, or redis containers.
if command -v docker &>/dev/null; then
  echo "Restarting gateway container..."
  docker compose up -d --no-deps gateway
  echo "Gateway restarted. All existing admin sessions are now invalid."
else
  echo "WARNING: docker not found — restart the gateway manually to apply the new token."
fi
