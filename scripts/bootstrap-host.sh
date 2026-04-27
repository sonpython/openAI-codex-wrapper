#!/usr/bin/env bash
# bootstrap-host.sh — provision a fresh Ubuntu 24.04 VM for codex-wrapper.
# Run as root (or sudo) on the target host.
#
# What it does:
#   1. Installs Docker + Compose plugin
#   2. Installs age (backup encryption)
#   3. Creates /opt/codex-wrapper and required data dirs
#   4. Clones the repo (or uses current dir)
#   5. Prompts for .env.production values
#   6. Runs codex login interactively (one-time human step)
#   7. Starts the stack
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/your-org/codex-wrapper/main/scripts/bootstrap-host.sh | sudo bash
#   # Or, after git clone:
#   sudo bash scripts/bootstrap-host.sh

set -euo pipefail

INSTALL_DIR="/opt/codex-wrapper"
REPO_URL="${REPO_URL:-https://github.com/your-org/codex-wrapper.git}"
CODEX_AUTH_DIR="/root/.codex"

info()  { echo "[bootstrap] $*"; }
error() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

# ── Require root ──────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run as root: sudo bash $0"

# ── OS check ──────────────────────────────────────────────────────────────────
. /etc/os-release 2>/dev/null || true
info "OS: ${PRETTY_NAME:-unknown}"

# ── 1. Install Docker ─────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    info "Docker already installed: $(docker --version)"
fi

# ── 2. Install age ────────────────────────────────────────────────────────────
if ! command -v age &>/dev/null; then
    info "Installing age..."
    AGE_VERSION="v1.1.1"
    curl -fsSL "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-amd64.tar.gz" \
        | tar -xz --strip-components=1 -C /usr/local/bin age/age age/age-keygen
    chmod +x /usr/local/bin/age /usr/local/bin/age-keygen
    info "age installed: $(age --version)"
else
    info "age already installed: $(age --version)"
fi

# ── 3. Create directories ─────────────────────────────────────────────────────
info "Creating data directories..."
mkdir -p \
    "${INSTALL_DIR}" \
    /var/lib/codex-wrapper/postgres \
    /var/lib/codex-wrapper/redis \
    /var/lib/caddy \
    "${CODEX_AUTH_DIR}"

chmod 700 "${CODEX_AUTH_DIR}"

# ── 4. Clone or update repo ───────────────────────────────────────────────────
if [[ -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
    info "Repo already present at ${INSTALL_DIR}, pulling latest..."
    git -C "${INSTALL_DIR}" pull --ff-only
else
    info "Cloning repo to ${INSTALL_DIR}..."
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

# ── 5. Bootstrap .env ─────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    info "Creating .env from .env.example..."
    cp .env.example .env
    chmod 600 .env
    info "IMPORTANT: Edit ${INSTALL_DIR}/.env before starting the stack."
    info "           Set ADMIN_TOKEN, BACKUP_AGE_RECIPIENT, BACKUP_S3_BUCKET, etc."
    read -r -p "Press ENTER after editing .env to continue..."
fi

# ── 6. codex login (interactive) ─────────────────────────────────────────────
info "Running 'codex login' — complete the browser auth flow when prompted."
info "(This stores ChatGPT session in ${CODEX_AUTH_DIR})"
docker run --rm -it \
    -v "${CODEX_AUTH_DIR}:/root/.codex" \
    --entrypoint codex \
    "$(grep 'codex-wrapper-gateway' "${INSTALL_DIR}/docker-compose.yml" | head -1 || echo 'node:22-alpine')" \
    login || true
info "codex login complete (or skipped)."

# ── 7. Start the stack ────────────────────────────────────────────────────────
info "Starting codex-wrapper stack..."
docker compose -f docker-compose.yml -f docker-compose.production.yml pull --quiet
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --wait

info "Bootstrap complete."
info "  Gateway health: http://localhost:8000/healthz (internal)"
info "  Grafana:        http://localhost:3000 (SSH tunnel: ssh -L 3000:localhost:3000 root@<host>)"
info "  Docs:           ${INSTALL_DIR}/docs/deployment-guide.md"
