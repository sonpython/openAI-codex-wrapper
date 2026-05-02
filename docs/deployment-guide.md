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
| Cloudflare account + domain | Public ingress via Tunnel | Yes (e.g. `openai.sonpython.com`) |

### Ports (UFW rules)

Cloudflare Tunnel (cloudflared) makes outbound-only connections to Cloudflare
edge — **no inbound ports required for public ingress**. The host can run
fully behind NAT / firewall.

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 22 | TCP | Inbound | SSH admin access (Grafana / Prometheus reached via SSH tunnel) |
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
| `CLOUDFLARED_TUNNEL_TOKEN` | Cloudflare Tunnel connector token | `eyJh...` (CF Zero Trust dashboard) |

### Optional production variables

| Variable | Default | Description |
|---|---|---|
| `BACKUP_S3_ENDPOINT` | AWS S3 | Override for R2/B2/MinIO |
| `PAGERDUTY_KEY` | (empty) | PagerDuty routing key; disables PD if unset |
| `AWS_DEFAULT_REGION` | `us-east-1` | S3 region |
| `OTEL_SAMPLER_RATIO` | `0.1` | Trace sampling (1.0 = 100%) |

### Security Notes

- **Grafana default creds (`admin/admin`)** must be rotated immediately after first login. Change via Grafana UI: User Menu → Change Password.
- **PROMETHEUS_URL** should use internal Docker network address in compose; external access via SSH tunnel only.
- **Volume retention:** `grafana_data` and `prometheus_data` are persisted across restarts; include in backup strategy.

---

## Public Ingress: Cloudflare Tunnel

Production deploys publish the API + admin UI through a **Cloudflare Tunnel**
(`cloudflared`). Cloudflare terminates TLS at the edge and routes the public
hostname to the gateway over an outbound-only tunnel — no inbound ports, no
ACME, no Let's Encrypt cert management.

### One-time Cloudflare setup

1. Add the domain (e.g. `sonpython.com`) to your Cloudflare account.
2. Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels**
   → **Create a tunnel**.
3. Choose connector type **Cloudflared**, name the tunnel
   (e.g. `codex-wrapper-prod`), then click **Save tunnel**.
4. CF shows install snippets — copy the **token** (long JWT starting `eyJh...`).
   This is the value of `CLOUDFLARED_TUNNEL_TOKEN`.
5. Skip the install step in CF dashboard (compose runs cloudflared for you).
6. Open the tunnel's **Public Hostname** tab → **Add a public hostname**:
   - Subdomain: `openai`
   - Domain: `sonpython.com`
   - Path: (empty)
   - Service Type: **HTTP**
   - URL: `gateway:8000`
   - Save.
7. The hostname is live in ≤30 s after tunnel connects from the host.

### Recommended: gate `/admin/*` behind Cloudflare Access

The admin UI accepts an `ADMIN_TOKEN` cookie, but exposing it openly invites
brute-force attempts. Add a Cloudflare Access policy:

- Zero Trust → **Access** → **Applications** → Add → **Self-hosted**
- Application name: `codex-wrapper admin`
- Subdomain: `openai`, Domain: `sonpython.com`, Path: `/admin*`
- Add policy: e.g. allow login via Google/GitHub for your email only.

Now `/admin/*` requires CF Access auth before reaching the gateway, in
addition to the existing token cookie.

### Internal-only services (Grafana, Prometheus, Postgres, Redis)

These are **not** exposed via Cloudflared and have no host ports. Reach them
via SSH tunnel from your laptop:

```bash
ssh -L 3000:grafana:3000 -L 9090:prometheus:9090 ops@<host>
# Then open http://localhost:3000 (Grafana) and http://localhost:9090 (Prom).
```

---

## TLS

Cloudflare terminates TLS at the edge with its managed certificate for
`*.sonpython.com` (free with any CF plan). Origin (gateway) speaks plain HTTP
on the Docker network — only `cloudflared` reaches it. No ACME, no certs to
rotate on the host.

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
