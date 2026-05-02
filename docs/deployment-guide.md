# Deployment Guide — codex-wrapper

**Scope:** Single-VM Docker Compose deploy, INTERNAL ONLY (v1).
**Target OS:** Ubuntu 24.04 LTS, kernel ≥ 5.13 (required for Landlock seccomp).
**Time to first request:** < 15 min from `git clone` on a pre-provisioned VM.

---

## Prerequisites

### VM Sizing

| Resource | Minimum | Recommended |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 8 GB | 16 GB |
| Disk (root) | 50 GB SSD | 100 GB SSD |
| OS | Ubuntu 24.04 LTS | Ubuntu 24.04 LTS |
| Kernel | ≥ 5.13 | ≥ 6.8 (Ubuntu 24.04 default) |

Postgres data and Redis AOF are stored on named Docker volumes (default: `/var/lib/docker/volumes/`). Point this to a fast SSD.

### Software (installed by bootstrap script)

- Docker Engine ≥ 26 + Compose plugin v2
- `age` ≥ 1.1.1 (backup encryption)
- `git` ≥ 2.34
- `curl`, `bash`

### External Services

| Service | Purpose | Required |
|---|---|---|
| S3 / Cloudflare R2 / Backblaze B2 | Encrypted backup storage | Yes |
| Slack Workspace | Alert notifications | Recommended |
| PagerDuty | On-call paging | Optional |
| ChatGPT account | codex CLI auth | **Yes — one account per worker** |
| DNS A record | `codex.internal` → VM IP | Yes (internal DNS or /etc/hosts) |

### Ports (UFW rules)

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 22 | TCP | Inbound | SSH admin access |
| 80 | TCP | Inbound | Caddy HTTP→HTTPS redirect |
| 443 | TCP | Inbound | Caddy HTTPS (API + admin UI) |
| 3001 | TCP | Inbound (via tunnel) | Grafana dashboards (local dev: :3001) |
| 9090 | TCP | Inbound (via tunnel) | Prometheus (local dev: :9090) |
| All others | — | Inbound | DENY |

---

## Environment Variables

Copy `.env.example` to `.env` and fill in all required values:

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

### Required for production

| Variable | Description | Example |
|---|---|---|
| `ADMIN_TOKEN` | Admin API bearer token (min 32 chars) | `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | Postgres password | `openssl rand -hex 24` |
| `BACKUP_AGE_RECIPIENT` | age public key for backup encryption | `age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p` |
| `BACKUP_S3_BUCKET` | S3/R2/B2 bucket name | `codex-wrapper-backups` |
| `SLACK_WEBHOOK_URL` | Incoming webhook URL | `https://hooks.slack.com/...` |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin password (rotate before prod) | `openssl rand -hex 16` |
| `PROMETHEUS_URL` | Prometheus URL for admin UI queries | `http://prometheus:9090` |
| `GHCR_ORG` | GitHub org/user for image pulls | `your-org` |
| `IMAGE_TAG` | Image tag to deploy | `v0.1.0` |

### Optional production variables

| Variable | Default | Description |
|---|---|---|
| `BACKUP_S3_ENDPOINT` | AWS S3 | Override for R2/B2/MinIO |
| `PAGERDUTY_KEY` | (empty) | PagerDuty routing key; disables PD if unset |
| `AWS_DEFAULT_REGION` | `us-east-1` | S3 region |
| `OTEL_SAMPLER_RATIO` | `0.1` | Trace sampling (1.0 = 100%) |
| `ACCESS_GATE_KIND` | `caddy-ip` | Documents chosen access gate (informational) |

### Security Notes

- **Grafana default creds (`admin/admin`)** must be rotated immediately after first login. Change via Grafana UI: User Menu → Change Password.
- **PROMETHEUS_URL** should use internal Docker network address in compose; external access via SSH tunnel only.
- **Volume retention:** `grafana_data` and `prometheus_data` are persisted across restarts; include in backup strategy.

---

## DNS / Access Gate Setup

### Option A — Caddy IP Allowlist (default)

1. Add an internal DNS A record: `codex.internal` → `<VM_IP>`
   - Or add to `/etc/hosts` on all client machines.
2. Edit `infra/Caddyfile.production` — replace `codex.internal` with your FQDN.
3. Adjust `@allowed` CIDR ranges to match your VPN/LAN subnets.

### Option B — Tailscale

1. Install Tailscale on the VM and all client devices.
2. Use the Tailscale MagicDNS hostname in `Caddyfile.production`.
3. Set UFW to allow only from `tailscale0` interface.

See `infra/access-gate/README.md` for full option comparison.

---

## TLS Certificate Provisioning

Caddy handles TLS automatically:

- **Public ACME domain** (domain has public DNS): Caddy requests Let's Encrypt cert via HTTP-01 challenge on port 80. No manual steps needed.
- **Truly internal domain** (`*.internal`, no public DNS): Configure `step-ca` as internal ACME CA:
  1. Install `step-ca` on the VM or a separate host.
  2. Set `acme_ca` in `Caddyfile.production` to `step-ca` directory URL.
  3. Distribute the root CA cert to all client browsers/machines.

Cert storage: persisted in the `caddy_data` Docker volume (survives restarts).

---

## First-Time Bootstrap Walk-Through

```bash
# 1. Clone the repo
git clone https://github.com/your-org/codex-wrapper.git /opt/codex-wrapper
cd /opt/codex-wrapper

# 2. Run bootstrap script (installs Docker, age, creates dirs)
sudo bash scripts/bootstrap-host.sh

# 3. Edit .env (script will pause and prompt)
#    Required: ADMIN_TOKEN, POSTGRES_PASSWORD, BACKUP_*, SLACK_WEBHOOK_URL

# 4. Complete codex login in browser (script prompts)

# 5. Confirm stack is healthy
docker compose -f docker-compose.yml -f docker-compose.production.yml ps
curl -sf http://localhost:8000/healthz

# 6. Run database migrations
docker compose exec gateway uv run alembic upgrade head

# 7. Create first API key
curl -X POST http://localhost:8000/admin/api-keys \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"ops-key","tier":"internal"}'

# 8. Test end-to-end (replace <key> with value from step 7)
curl -sf https://codex.internal/v1/models \
  -H "Authorization: Bearer <key>"
```

---

## Update Procedure

1. Ensure compat suite is green on `main`: check `.github/workflows/compat.yml`.
2. Tag the release:
   ```bash
   git tag v0.x.y -m "Release v0.x.y"
   git push --tags
   ```
3. GH Actions `deploy.yml` triggers automatically:
   - Runs compat gate
   - Builds + pushes images to GHCR
   - SSH-deploys to production VM
4. Monitor deployment in GH Actions and Grafana.
5. Verify: `curl -sf https://codex.internal/v1/models`

**Rollback:** Re-tag the previous version or re-run `deploy.yml` on the previous tag.
Manual rollback on VM:
```bash
cd /opt/codex-wrapper
export IMAGE_TAG=v0.x.y-1
docker compose -f docker-compose.yml -f docker-compose.production.yml pull
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

---

## Backup / Restore Drill Checklist

Run quarterly. Goal: < 30 min end-to-end restore on a sandbox VM.

- [ ] Confirm backup ran last night: check S3 bucket for today's `.dump.age` object
- [ ] Spin up sandbox VM (or reuse staging)
- [ ] Copy `~/.config/age/key.txt` to sandbox
- [ ] Run `postgres-restore.sh` against latest backup key
- [ ] Verify: `SELECT count(*) FROM api_keys` matches production count
- [ ] Document result in DR log (`docs/dr-log.md`)
- [ ] Schedule next drill (within 90 days)

---

## Monitoring URLs (VPN / SSH Tunnel Only)

All monitoring UIs are on the internal Docker network. Access via SSH tunnel:

```bash
# Open all tunnels at once (note: Grafana mapped to port 3001):
ssh -L 3001:localhost:3001 \   # Grafana (default admin/admin, ROTATE PASSWORD!)
    -L 9090:localhost:9090 \   # Prometheus
    -L 9093:localhost:9093 \   # Alertmanager
    root@<VM_IP>
```

| Service | URL | Credentials |
|---|---|---|
| **Grafana** | http://localhost:3001 | `admin` / `$GRAFANA_ADMIN_PASSWORD` (rotate immediately) |
| **Prometheus** | http://localhost:9090 | none (internal only) |
| **Alertmanager** | http://localhost:9093 | none (internal only) |

**Grafana Dashboards (auto-provisioned):**
- **System Overview** — Request rate, error rate, latency p50/p95/p99, queue depth, active jobs
- **API Endpoints** — Per-route request rate, p95 latency, top-10 routes, status codes
- **Codex CLI** — Event types, subprocess duration percentiles, top event types
