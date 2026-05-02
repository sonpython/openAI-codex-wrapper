# Operations Runbook — codex-wrapper

**Audience:** On-call engineers with SSH access to the production VM.
**Stack:** Single-VM Docker Compose, Ubuntu 24.04, Cloudflare Tunnel (cloudflared), Postgres 16, Redis 7, Loki/Tempo/Prometheus, Grafana.
**Install dir:** `/opt/codex-wrapper`

---

## Quick Reference

| Alert | Runbook section |
|---|---|
| `CodexSessionUnhealthy` | [§9 Recover from ChatGPT session expiry](#9-recover-from-chatgpt-session-expiry) |
| `P95LatencyHigh` | [§7 Investigate stuck job](#7-investigate-stuck-job) |
| `RateLimitRejectionSpike` | [§8 Add new tier / change rate-limit](#8-add-new-tier--change-rate-limit) |
| `ArqQueueDepthHigh` | [§7 Investigate stuck job](#7-investigate-stuck-job) |
| `WorkspaceDiskFull` | [§6 Restore from backup](#6-restore-from-backup) + manual rm |
| `ContainerRestartLoop` | [§7 Investigate stuck job](#7-investigate-stuck-job) |
| `PostgresConnectionFailures` | [§6 Restore from backup](#6-restore-from-backup) |

---

## 1. Bootstrap Host (interactive `codex login`)

**Trigger:** First-time deploy on a new VM.

**Steps:**

1. SSH into the VM as root: `ssh root@<VM_IP>`
2. Run bootstrap script:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/your-org/codex-wrapper/main/scripts/bootstrap-host.sh | bash
   ```
3. When prompted, edit `/opt/codex-wrapper/.env` with real values (ADMIN_TOKEN, BACKUP_*, SLACK_WEBHOOK_URL, etc.).
4. The script runs `codex login` inside a container — complete the browser OAuth flow.
5. Verify auth file was created: `ls -la ~/.codex/auth.json`

**Verify:**
```bash
curl -sf http://localhost:8000/healthz   # returns {"status":"ok"}
```

**Post-mortem:** If codex login fails, see §9.

---

## 2. First-Time Deploy Steps

**Trigger:** New environment (staging or production).

**Steps:**

1. Complete §1 bootstrap first.
2. Verify `.env` is complete — required fields:
   ```
   ADMIN_TOKEN, DATABASE_URL, REDIS_URL,
   BACKUP_AGE_RECIPIENT, BACKUP_S3_BUCKET,
   SLACK_WEBHOOK_URL, GRAFANA_ADMIN_PASSWORD
   ```
3. Run database migrations:
   ```bash
   cd /opt/codex-wrapper
   docker compose exec gateway uv run alembic upgrade head
   ```
4. Create first admin API key:
   ```bash
   curl -X POST http://localhost:8000/admin/api-keys \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"ops","tier":"internal"}'
   ```
5. Confirm Grafana loads: `ssh -L 3000:localhost:3000 root@<host>` → open http://localhost:3000
6. Fire a test alert to verify Alertmanager → Slack pipeline:
   ```bash
   curl -X POST http://localhost:9093/api/v2/alerts \
     -H 'Content-Type: application/json' \
     -d '[{"labels":{"alertname":"TestAlert","severity":"warn"},"annotations":{"summary":"Bootstrap test"}}]'
   ```

**Verify:** Slack `#codex-alerts` receives the test alert.

---

## 3. Update Codex CLI Version

**Trigger:** New `@openai/codex` release; drift detection cron fails.

**Steps:**

1. Test new version in staging first (mandatory):
   ```bash
   # On staging VM:
   export NEW_VERSION=0.x.y
   docker run --rm ghcr.io/your-org/codex-wrapper-gateway:staging \
     codex --version
   ```
2. Update `Dockerfile.gateway` and `Dockerfile.worker`:
   ```dockerfile
   # Replace pinned version:
   RUN npm install -g @openai/codex@0.x.y
   ```
3. Run compat suite on the branch: `make test-compat`
4. If compat passes, tag a new release: `git tag v0.x.y && git push --tags`
5. GH Actions deploy workflow runs automatically (see `deploy.yml`).
6. After deploy, verify: `curl -sf https://codex.internal/v1/models`

**Rollback:** Push previous tag to trigger redeploy:
```bash
git tag v0.x.y-prev-redeploy && git push --tags
```

---

## 4. Rotate Compromised API Key

**Trigger:** Security incident; suspected key leak.

**Steps:**

1. Immediately revoke the compromised key:
   ```bash
   curl -X DELETE https://codex.internal/admin/api-keys/<key-id> \
     -H "Authorization: Bearer $ADMIN_TOKEN"
   ```
2. If ADMIN_TOKEN is compromised, update it immediately:
   ```bash
   # On VM:
   cd /opt/codex-wrapper
   NEW_TOKEN=$(openssl rand -hex 32)
   sed -i "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=${NEW_TOKEN}/" .env
   docker compose -f docker-compose.yml -f docker-compose.production.yml up -d gateway
   echo "New ADMIN_TOKEN: ${NEW_TOKEN}"  # store in password manager
   ```
3. Audit recent activity:
   ```bash
   docker compose exec postgres psql -U codex codex_wrapper \
     -c "SELECT * FROM audit_log WHERE api_key_id='<id>' ORDER BY created_at DESC LIMIT 50;"
   ```
4. Notify affected users; issue new keys.
5. Review Loki logs for anomalous usage: Grafana → Explore → Loki query:
   ```
   {service="gateway"} |= "<compromised-key-prefix>"
   ```

**Verify:** Revoked key returns 401 on next request.

---

## 5. Drain Worker for Maintenance

**Trigger:** Planned maintenance; worker upgrade; memory leak investigation.

**Steps:**

1. Stop accepting new jobs (pause the queue):
   ```bash
   # Set a maintenance flag — new /v1/codex/jobs POST returns 503.
   # For now: stop the worker container (in-flight jobs will timeout per JOB_TIMEOUT_SECONDS).
   docker compose -f docker-compose.yml -f docker-compose.production.yml stop worker
   ```
2. Wait for in-flight jobs to complete (check queue depth):
   ```bash
   docker compose exec redis redis-cli LLEN arq:queue:default
   # Wait until 0, or until JOB_TIMEOUT_SECONDS (900s) elapses.
   ```
3. Perform maintenance (image update, config change, etc.).
4. Restart worker:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml up -d worker
   ```

**Verify:** `docker compose logs worker --tail=20` shows worker polling queue.

---

## 6. Restore from Backup

**Trigger:** `PostgresConnectionFailures` page; DB corruption; accidental data deletion.

**Steps:**

1. Identify the backup to restore:
   ```bash
   aws s3 ls s3://$BACKUP_S3_BUCKET/postgres/ --endpoint-url $BACKUP_S3_ENDPOINT \
     | sort | tail -10
   ```
2. Stop the gateway and worker to prevent writes during restore:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml stop gateway worker
   ```
3. Run the restore script:
   ```bash
   cd /opt/codex-wrapper
   docker compose run --rm \
     -e AGE_IDENTITY_FILE=/root/.config/age/key.txt \
     -v ~/.config/age:/root/.config/age:ro \
     postgres-backup \
     /usr/local/bin/postgres-restore.sh "postgres/codex_wrapper-<TIMESTAMP>.dump.age"
   ```
4. Verify row counts (script prints them automatically).
5. Restart services:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml up -d gateway worker
   ```
6. Verify: `curl -sf https://codex.internal/v1/models`

**Post-mortem:** File a DR drill report. Next quarterly drill scheduled within 90 days.

---

## 7. Investigate Stuck Job

**Trigger:** `ArqQueueDepthHigh`, `P95LatencyHigh`, `ContainerRestartLoop`, user complaint.

**Steps:**

1. Check queue depth:
   ```bash
   docker compose exec redis redis-cli LLEN arq:queue:default
   ```
2. List in-progress jobs:
   ```bash
   docker compose exec redis redis-cli KEYS "arq:job:*" | head -20
   ```
3. Check worker logs for errors:
   ```bash
   docker compose logs worker --tail=100 --follow
   ```
4. Check Loki for job-id specific logs (Grafana → Explore):
   ```
   {service="worker"} |= "<job_id>"
   ```
5. If worker is deadlocked, restart it:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.production.yml restart worker
   ```
6. Cancel a specific stuck job via admin API:
   ```bash
   curl -X DELETE https://codex.internal/v1/codex/jobs/<job_id> \
     -H "Authorization: Bearer <api-key>"
   ```
7. If codex subprocess is leaking, check for orphaned processes:
   ```bash
   docker compose exec worker ps aux | grep codex
   ```

**Verify:** Queue depth returns to 0; P95 latency drops below 5s.

---

## 8. Add New Tier / Change Rate-Limit

**Trigger:** `RateLimitRejectionSpike`; product change; new customer tier.

**Steps:**

1. Check current tier configs in `src/gateway/rate_limit/`:
   ```bash
   grep -r "TIER_CONFIGS" src/gateway/rate_limit/
   ```
2. Edit tier definition (follow phase-06 conventions):
   ```python
   # src/gateway/rate_limit/tier-configs.py
   TIER_CONFIGS["enterprise"] = TierConfig(rpm=5000, tpm=500_000, concurrent=20, monthly=None)
   ```
3. Deploy updated image (tag + push → GH Actions).
4. Update affected API keys to new tier via admin API:
   ```bash
   curl -X PATCH https://codex.internal/admin/api-keys/<id> \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -d '{"tier":"enterprise"}'
   ```
5. For emergency rate-limit relief (before deploy), increase limits in Redis:
   — Not supported directly; deploy is the correct path.

**Verify:** Test request from affected key no longer receives 429.

---

## 9. Recover from ChatGPT Session Expiry

**Trigger:** `CodexSessionUnhealthy` alert; users receiving 503 "codex session unhealthy".

**Symptom:** `codex_session_unhealthy` gauge == 1 for > 5 min. Gateway health probe
(`codex auth status`) returning non-zero exit code.

**Steps:**

1. Confirm the alert:
   ```bash
   docker compose exec gateway codex auth status
   # Expected when healthy: exit 0 with JSON containing "loggedIn": true
   ```
2. SSH into the production VM:
   ```bash
   ssh root@<VM_IP>
   ```
3. Re-run `codex login` interactively:
   ```bash
   docker compose exec -it gateway codex login
   # Complete the browser OAuth flow — paste the URL into a browser.
   ```
4. Verify the session is healthy:
   ```bash
   docker compose exec gateway codex auth status
   ```
5. Force the health-check gauge to re-evaluate (it polls every `CODEX_SESSION_POLL_INTERVAL_SECONDS`):
   ```bash
   # Restart gateway to force immediate re-probe:
   docker compose -f docker-compose.yml -f docker-compose.production.yml restart gateway
   ```
6. Confirm `codex_session_unhealthy` gauge returns to 0 in Grafana.
7. Confirm alert resolves in Alertmanager (within 5 min).

**Verify:** `curl -sf https://codex.internal/v1/models` returns 200.

**Post-mortem:**
- Note session duration; ChatGPT sessions typically last 7–30 days.
- Schedule next expected expiry in on-call calendar.
- Consider multi-account session pool (phase 11 roadmap item).

---

## 10. Triage Real-Codex Drift Alert

**Trigger:** Weekly drift cron (`compat-real-codex.yml`) fails; GH issue auto-created.

**Steps:**

1. Open the failing GH Actions run linked in the auto-created issue.
2. Identify which JSONL event types are drifting (look for `DRIFT WARNING` in test output).
3. Run locally to reproduce:
   ```bash
   CODEX_REAL=1 uv run pytest tests/compat/test-real-codex-drift.py -v
   ```
4. If new event types appear in codex output:
   - Add them to `src/codex/events.py`
   - Add fixture to `tests/fixtures/jsonl/`
   - Update `tests/fixtures/canned-prompts.json`
5. If breaking schema change (field renamed/removed):
   - Keep codex version pinned in Dockerfiles
   - Open a blocking upgrade issue for next sprint
6. If non-breaking (new optional field):
   - Update parser; bump codex pin; re-run compat suite.

**Verify:** `make test-compat` passes on the fix branch.

---

## Admin UI Access

### Login

The admin web UI is available at `http://localhost:8000/admin/ui` (dev) or `https://<your-tunnel-host>/admin/ui` (prod via Cloudflare Tunnel, e.g. `https://openai.sonpython.com/admin/ui`).

1. Open the login URL in a browser.
2. Enter the `ADMIN_TOKEN` value from `.env`.
3. A signed HttpOnly session cookie is set; TTL defaults to 8 hours (`ADMIN_SESSION_TTL_SECONDS`).

**Pages available:**
| Page | Path |
|---|---|
| Dashboard (live KPIs, 5s auto-refresh) | `/admin/ui/` |
| API Keys CRUD | `/admin/ui/keys` |
| Tier editor | `/admin/ui/tiers` |
| Users list + usage aggregates | `/admin/ui/users` |
| Job inspector | `/admin/ui/jobs` |
| Audit log viewer | `/admin/ui/audit` |

### Rotate Admin Token

Use the provided script — it generates a new 32-byte hex token, updates `.env`, and restarts only the gateway container:

```bash
# From the project root on the host (not inside Docker):
bash scripts/rotate-admin-token.sh
```

The script:
1. Generates `NEW=$(openssl rand -hex 32)`
2. Writes `ADMIN_TOKEN=$NEW` to `.env` (backs up old `.env` as `.env.bak`)
3. Runs `docker compose up -d --no-deps gateway` to apply without service interruption
4. **All existing admin sessions are immediately invalidated** (session signing key = admin token)

Dry-run (print new token only, no changes):
```bash
bash scripts/rotate-admin-token.sh --dry-run
```

After rotating, delete `.env.bak` once you have verified the new token works:
```bash
rm .env.bak
```

### Session TTL

Default session TTL is **8 hours** (`ADMIN_SESSION_TTL_SECONDS=28800`). Adjust in `.env` and restart gateway. Sessions are stored in Redis; a Redis flush also invalidates all sessions.

---

## Cloudflare Tunnel Operations

### Tunnel status

```bash
# Connector logs (last 50 lines)
docker compose logs --tail 50 cloudflared

# Look for: "Registered tunnel connection" + "Updated to new configuration"
```

### Verify public hostname is reachable

```bash
# Should return 401 (no bearer) or 200 — anything but a connection error proves
# Cloudflare → tunnel → gateway path is working.
curl -I https://openai.sonpython.com/healthz
```

### Reload ingress rules

Public hostname routing is configured in **Cloudflare Zero Trust dashboard**
→ Tunnels → (your tunnel) → Public Hostname tab. Changes propagate to the
connector in ~30 s; no host-side restart needed.

### Rotate tunnel token

1. Zero Trust → Networks → Tunnels → (your tunnel) → **Refresh token**.
2. Copy the new token, update `CLOUDFLARED_TUNNEL_TOKEN` in `.env`.
3. `docker compose -f docker-compose.yml -f docker-compose.production.yml up -d cloudflared`.

### Bypass tunnel for incident debugging

If the tunnel goes down and the public host is unreachable, expose the
gateway port on the host temporarily:

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d \
  --scale cloudflared=0
docker run -d --network codex-wrapper_default --name gw-tmp \
  -p 8000:8000 --link gateway alpine/socat \
  TCP-LISTEN:8000,fork,reuseaddr TCP:gateway:8000
```

Restore by deleting the socat container and `docker compose up -d cloudflared`.

---

## Prometheus & Grafana Operations

### Access Grafana Dashboard

**Local dev (docker-compose):**
```bash
open http://localhost:3001
# Default: admin / admin (CHANGE PASSWORD!)
```

**Production (via SSH tunnel):**
```bash
ssh -L 3001:localhost:3001 root@<VM_IP>
# Then open http://localhost:3001
```

**Auto-provisioned dashboards:**
- **System Overview** — Request rate, error rate, latency percentiles, queue depth, active jobs
- **API Endpoints** — Per-route metrics, top endpoints, status codes
- **Codex CLI** — Event types, subprocess duration, event taxonomy

### Check Prometheus Targets

Health of metrics collection. Verify scrape targets are healthy:

```bash
# Via SSH tunnel or local (if exposed):
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job, endpoint, health}'

# Expected output shows `gateway:8000/_internal/metrics` with health="up"
```

### Reload Prometheus Config (if modified)

If you edit `prometheus.yml` and want to apply changes without restart:

```bash
curl -X POST http://localhost:9090/-/reload
```

### Storage & Retention

- **Default retention:** 15 days (configurable via `docker-compose.yml` `--storage.tsdb.retention.time`)
- **Disk usage:** Monitor `prometheus_data` volume size
- **Backup:** Included in daily `pg_dump` backup job (though Prometheus is ephemeral; rebuild from metrics)

---

## Common Error Codes

| Code | Symptom | Cause | First Action |
|---|---|---|---|
| `codex_session_expired` | 503 on all `/v1/*` routes; `codex_session_unhealthy==1` | `~/.codex` token revoked or refresh broken | §9 Recover from ChatGPT session expiry |
| `codex_rate_limited` | 429 from gateway with `"source":"codex_cli"` | Account-level 429 from ChatGPT backend | §8 — raise tier or pool accounts |
| `workspace_disk_full` | Jobs failing with disk error; `WorkspaceDiskFull` alert | Janitor lagging or runaway clone | §6 + `docker compose exec worker find /workspaces -maxdepth 1 -mmin +60 -exec rm -rf {} +` |
| `redis_connection_lost` | 503 on rate-limited routes; arq jobs not dequeuing | Redis container OOM or crash | `docker compose restart redis` → check `docker compose logs redis` → investigate memory usage |
