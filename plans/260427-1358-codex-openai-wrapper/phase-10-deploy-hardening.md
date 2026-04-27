# Phase 10: Deploy Hardening

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§3 architecture, §7 risks, §9 metrics, §11 single-VM start)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (§5 auth disk state — bootstrap step)
- Phase 00: phase-00-bootstrap.md (Caddy dev profile — extends here for prod)
- Phase 07: phase-07-observability.md (metrics/dashboards consumed by Prometheus + alerts)
- Phase 08: phase-08-hardening.md (audit log, admin endpoints, session monitoring — alert sources)
- Phase 09: phase-09-openai-sdk-compat-tests.md (compat suite gates deploy)
- Project rules: ../../.claude/rules/development-rules.md

## Overview
- Priority: high
- Status: pending
- Effort: M
- Description: **INTERNAL ONLY** deploy on a single VM via Docker Compose. Internal access gate (one of: Cloudflare Access / Tailscale / Caddy IP allowlist — pick in step 0), TLS via Caddy (internal CA for `*.internal` domain or ACME for an internal-only public DNS), Postgres backup with `age` encryption to S3/B2, log shipping to **Loki**, Prometheus + Grafana + **Tempo** (traces) + Alertmanager, GitHub Actions deploy pipeline, weekly real-codex drift cron, written operations runbook. After this phase: system survives reboots, outages, ChatGPT session refresh windows, and is **verified NOT reachable from public Internet**.

## Key Insights
- **v1 INTERNAL ONLY** (locked 2026-04-27, plan.md scope). No public TLS exposure. Access gate is a hard requirement — phase-10 is incomplete until external port-scan returns zero open ports.
- Single-VM Docker Compose intentional: small internal team, K8s deferred indefinitely.
- Caddy with internal CA (or ACME via DNS-01 if internal domain has ACME-validatable DNS): zero TLS config drift; renewal auto-managed.
- 1-hour Caddy request timeout REQUIRED for SSE (default 60s kills long generations). Most common deploy footgun.
- **Stack picks locked**: Loki (logs), Tempo (traces), age (backup encryption). Loki + Tempo = Grafana-native, single-pane-of-glass with Prometheus metrics. age picked over gpg for simpler key mgmt (single recipient pubkey, no keyring).
- Backup chain: `pg_dump | age -r $RECIPIENT | aws s3 cp - s3://...`. KISS, auditable, restore drill quarterly.
- Image immutability via tag pin: `ghcr.io/<org>/codex-wrapper:vX.Y.Z` where Z encodes both wrapper version AND codex CLI version.
- **Drift defense**: weekly GH Actions cron `compat-real-codex.yml` runs `@openai/codex@latest` against canned fixtures (phase-09 deliverable). Fails loud → Slack alert → manual triage → bump pin or freeze upgrade.
- Runbook part of deliverable, not optional. Alerts without runbook entries are noise.

## Requirements

### Functional
- **Internal access gate enforced**: wrapper unreachable from public Internet. One of:
  - **Cloudflare Access** (recommended for org with Cloudflare): Zero Trust app, OAuth/Google SSO, free tier ≤ 50 users.
  - **Tailscale** (recommended for small dev team): MagicDNS + ACL, free for ≤ 100 devices.
  - **Caddy IP allowlist + WireGuard VPN** (no SaaS dependency): `@allowed { remote_ip 10.0.0.0/8 192.168.0.0/16 }` directive.
  Pick exactly one in step 0; document choice in `.env.example` and runbook.
- HTTPS-only on internal domain; HTTP redirects 301 → HTTPS.
- TLS via Caddy: internal CA (smallstep `step-ca`) for `*.internal` OR ACME (DNS-01) if domain is internal-but-DNS-publicly-resolvable.
- Postgres daily backup → S3/B2, **age-encrypted** (single recipient pubkey from `BACKUP_AGE_RECIPIENT`), 30-day retention.
- Redis AOF persistence + daily volume backup (age-encrypted to same S3 prefix).
- Prometheus scrapes gateway+worker metrics on internal `:9090`; Grafana renders phase-07 dashboards.
- **Tempo** receives OTLP traces from gateway+worker via otel-collector; Grafana data source linked.
- **Loki** receives JSON logs via Promtail sidecar (Docker JSON file driver → Promtail → Loki); 30-day retention.
- Alertmanager fires on 7 named rules (table below) → Slack webhook (PagerDuty optional).
- GitHub Actions builds tagged images, pushes to GHCR, ssh-deploys via `compose pull && compose up -d`.
- **Weekly real-codex smoke cron** (`compat-real-codex.yml`): every Sunday 03:00 UTC, runs `@openai/codex@latest` against `tests/fixtures/canned-prompts.json` in disposable container, asserts JSONL parser output matches expected events. On fail → Slack alert + GH issue auto-created.
- Runbook covers 10 named operations (table below) and is committed to repo.

### Non-Functional
- Cold deploy on fresh VM: < 15 min from `git clone` to first 200 OK on `/v1/models`.
- TLS handshake p95 < 200ms.
- Backup completion < 5 min for 10GB DB.
- Restore exercise (documented + practiced) < 30 min.
- Deploy via GH Actions: < 5 min from tag to live.
- All scripts ≤ 200 LOC.

## Architecture

```
DNS:
  api.example.com → A → VM public IP

VM (Ubuntu 24.04, kernel ≥ 5.13 for Landlock):
  /opt/codex-wrapper/
    docker-compose.yml                 (extends base)
    docker-compose.production.yml      (overlay: backups, log-shipper, otel-remote)
    Caddyfile.production
    .env                               (perms 0600, root-only)
    /var/lib/postgres/                 (volume)
    /var/lib/redis/                    (volume)
    /var/lib/caddy/                    (cert storage volume)
    /var/lib/loki/                     (if self-hosted)

containers (compose):
  caddy           :80, :443    public ingress + ACME
  gateway         :8000        Docker network only
  worker          (no ports)
  postgres:16     :5432        Docker network only
  redis:7         :6379        Docker network only
  prometheus      :9091        Docker network only (admin VPN access)
  grafana         :3000        VPN/SSH-tunnel only
  alertmanager    :9093        Docker network only
  otel-collector  :4317        Docker network only
  log-shipper     —            sidecar (vector/promtail)
  postgres-backup —            cron container (daily)

flows:
  client → :443 (Caddy) → :8000 (gateway)
  client → :443 + WebUI/admin → DENIED (only /v1/* allowed publicly)
  prometheus → :9090 (gateway internal metrics) → scrape every 15s
  alertmanager → webhook → Slack/PagerDuty
  log-shipper → reads docker logs (json file driver) → Loki/CloudWatch
  postgres-backup → pg_dump → age encrypt → s3 cp → B2/S3
```

### Alert rules

| Alert | Trigger | Severity | Runbook |
|---|---|---|---|
| `codex_session_unhealthy` | gauge==1 for 5 min | page | "Recover from ChatGPT session expiry" |
| `p95_latency_high` | `http_request_duration_seconds:p95{route=/v1/chat/completions}` > 5s for 10 min | warn | "Investigate slow chat completions" |
| `rate_limit_rejections_spike` | rate(`rate_limit_rejections_total`[5m]) > 10/s for 5 min | warn | "Hot key or DDoS" |
| `arq_queue_depth_high` | `arq_queue_depth` > 100 for 10 min | warn | "Drain or scale workers" |
| `workspace_disk_full` | `workspace_disk_bytes / mount_total_bytes` > 0.8 | page | "Clear stale workspaces" |
| `container_restart_loop` | `kube_pod_container_status_restarts_total` (or compose equiv) > 3/15min | page | "Investigate stuck container" |
| `postgres_connection_failures` | rate(`db_query_errors_total`[5m]) > 5/s for 5 min | page | "Postgres incident" |

### Runbook entries

| # | Operation | Trigger |
|---|---|---|
| 1 | Bootstrap host (interactive `codex login`) | first-time deploy |
| 2 | First-time deploy steps | new env |
| 3 | Update Codex CLI version (test in staging first) | new codex release |
| 4 | Rotate compromised API key | security incident |
| 5 | Drain worker for maintenance | planned ops |
| 6 | Restore from backup | DB corruption / loss |
| 7 | Investigate stuck job | user complaint |
| 8 | Add new tier / change rate-limit | product change |
| 9 | Recover from ChatGPT session expiry | alert paged |

### Common error codes (for runbook)

| Code | Cause | First action |
|---|---|---|
| `codex_session_expired` | `~/.codex` token revoked or refresh broken | runbook §9 |
| `codex_rate_limited` | account-level 429 from ChatGPT | runbook §8 (raise tier or pool) |
| `workspace_disk_full` | janitor lagging or runaway clones | runbook §6 + manual rm |
| `redis_connection_lost` | Redis container OOM or crash | `compose restart redis` + investigate logs |

## Related Code Files

### To create
- `Caddyfile.production` — full prod Caddyfile.
- `docker-compose.production.yml` — overlay extending base compose.
- `infra/backup/postgres-backup.sh` (≤ 100 LOC) — `pg_dump | age | aws s3 cp` cron script.
- `infra/backup/postgres-restore.sh` (≤ 80 LOC) — reverse: download + decrypt + psql.
- `infra/backup/redis-backup.sh` (≤ 60 LOC) — `redis-cli BGSAVE` + RDB upload.
- `infra/alerting/prometheus.yml` — scrape config for gateway+worker+postgres-exporter+redis-exporter.
- `infra/alerting/prometheus-rules.yml` — 7 alert rules above.
- `infra/alerting/alertmanager.yml` — webhook routing (Slack/PagerDuty).
- `infra/alerting/grafana-provisioning/datasources.yml`
- `infra/alerting/grafana-provisioning/dashboards.yml` — references phase-07 dashboards.
- `infra/log-shipper/vector.toml` (or `promtail-config.yml`) — Docker → Loki/CloudWatch.
- `.github/workflows/deploy.yml` — build + push + ssh-deploy on tag.
- `.github/workflows/release.yml` — release notes + GHCR tag.
- `docs/operations-runbook.md` — 9 operations + 4 error codes.
- `docs/deployment-guide.md` — first-time setup, env, DNS, host hardening.
- `scripts/bootstrap-host.sh` (≤ 100 LOC) — fresh-VM setup: docker, compose, codex login, dirs.

### To modify
- `docker-compose.yml` — add `restart: unless-stopped` to all services; add Docker log driver `local` (max-size 100m, max-file 10).
- `src/settings.py` — add `WEBHOOK_ALERT_URL`, `WEBHOOK_ALERT_KIND` (already in phase 08), `BACKUP_S3_BUCKET`, `BACKUP_AGE_RECIPIENT`.
- `.env.example` — document prod-only vars.
- `Makefile` — `deploy`, `backup`, `restore`, `runbook` (open in browser) targets.
- `README.md` — "Production deploy" section linking to deployment-guide + runbook.

### To delete
(none)

## Implementation Steps

1. **Caddyfile.production** —
   ```
   {
     email ops@example.com
     # ACME via Let's Encrypt is default; HTTP-01 challenge on :80
   }

   api.example.com {
     encode gzip zstd
     header {
       Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
       X-Content-Type-Options nosniff
       X-Frame-Options DENY
       Referrer-Policy strict-origin-when-cross-origin
     }

     # Edge rate-limit fallback (1000/min/IP)
     rate_limit {
       zone public_zone { key {remote_host} window 1m events 1000 }
     }

     # Public path = /v1/* only
     handle_path /v1/* {
       reverse_proxy gateway:8000 {
         transport http {
           response_header_timeout 1h    # critical for SSE streams
           read_timeout 1h
         }
         flush_interval -1                # disable buffering for SSE
       }
     }

     # Block everything else publicly
     handle {
       respond 404
     }

     log {
       output file /var/log/caddy/access.log {
         roll_size 100mb
         roll_keep 10
       }
       format json
     }
   }

   # 80 → 443 redirect handled by Caddy default
   ```

2. **docker-compose.production.yml** — overlay:
   - `restart: unless-stopped` on all services.
   - `logging: { driver: local, options: { max-size: 100m, max-file: 10 } }` everywhere.
   - Add `prometheus`, `grafana`, `alertmanager`, `postgres-backup` (cron container), `log-shipper` (vector or promtail), `postgres-exporter`, `redis-exporter`.
   - Caddy: bind `0.0.0.0:80` and `:443`; mount `/var/lib/caddy` for cert persistence.
   - Bind ports: only Caddy public; everything else Docker network internal.

3. **Postgres backup script** — `postgres-backup.sh`:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   TS=$(date -u +%Y%m%d-%H%M%S)
   pg_dump -h postgres -U "$PGUSER" -d codex_wrapper -Fc \
     | age -r "$BACKUP_AGE_RECIPIENT" \
     | aws s3 cp - "s3://$BACKUP_S3_BUCKET/postgres/codex_wrapper-$TS.dump.age"
   # Retention: lifecycle policy on bucket (30 days)
   ```
   Cron container: `crond -f` running 02:00 UTC daily.

4. **Postgres restore script** — `postgres-restore.sh`:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   KEY="$1"  # e.g., postgres/codex_wrapper-20260427-020000.dump.age
   aws s3 cp "s3://$BACKUP_S3_BUCKET/$KEY" - \
     | age -d -i ~/.config/age/key.txt \
     | pg_restore -h postgres -U "$PGUSER" -d codex_wrapper --clean --if-exists
   ```

5. **Redis persistence** — Redis config `redis.conf`:
   ```
   appendonly yes
   appendfsync everysec
   save 300 100   # RDB every 5min if 100 keys changed
   ```
   Daily volume backup script copies `dump.rdb` + AOF to S3.

6. **Workspace volume** — `tmpfs` mount with size limit 10GB:
   ```yaml
   volumes:
     workspaces: { driver: local, driver_opts: { type: tmpfs, device: tmpfs, o: "size=10g,uid=1000" } }
   ```
   Janitor (phase 08) handles cleanup.

7. **Prometheus scrape config** — `infra/alerting/prometheus.yml`:
   - Scrape `gateway:9090` (internal metrics port from phase 07), `worker:9090`, `postgres-exporter:9187`, `redis-exporter:9121`, `caddy:2019/metrics`.
   - 15s scrape interval; 30 day retention via `--storage.tsdb.retention.time=30d`.
   - Load `prometheus-rules.yml`.

8. **Alert rules** — `infra/alerting/prometheus-rules.yml`:
   ```yaml
   groups:
   - name: codex-wrapper
     rules:
     - alert: CodexSessionUnhealthy
       expr: codex_session_unhealthy == 1
       for: 5m
       labels: { severity: page }
       annotations:
         summary: "Codex session expired"
         runbook_url: "https://github.com/{org}/{repo}/blob/main/docs/operations-runbook.md#9-recover-from-chatgpt-session-expiry"
     # ... 6 more rules per §Alert rules table
   ```

9. **Alertmanager** — `infra/alerting/alertmanager.yml`: route by severity:
   - `severity=page` → PagerDuty webhook (env: `PAGERDUTY_KEY`).
   - `severity=warn` → Slack webhook (env: `SLACK_WEBHOOK_URL`).
   - Group by `alertname`, repeat after 4h.

10. **Log shipper** — Vector config (preferred for flexibility):
    ```toml
    [sources.docker]
    type = "docker_logs"

    [transforms.parse_json]
    type = "remap"
    inputs = ["docker"]
    source = '''
    . = parse_json!(.message)
    '''

    [sinks.loki]
    type = "loki"
    inputs = ["parse_json"]
    endpoint = "http://loki:3100"
    labels = { service = "{{ service }}", env = "{{ env }}" }
    ```
    Sensitive fields already redacted at structlog layer (phase 07).

11. **GH Actions deploy workflow** — `.github/workflows/deploy.yml`:
    ```yaml
    on:
      push:
        tags: ['v*']
    jobs:
      deploy:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: echo "${{ secrets.GHCR_TOKEN }}" | docker login ghcr.io -u {user} --password-stdin
          - run: |
              docker buildx bake -f docker-bake.hcl --push \
                --set "*.tags=ghcr.io/{org}/codex-wrapper:${GITHUB_REF_NAME}"
          - name: deploy via ssh
            uses: appleboy/ssh-action@v1
            with:
              host: ${{ secrets.PROD_HOST }}
              key: ${{ secrets.PROD_SSH_KEY }}
              script: |
                cd /opt/codex-wrapper
                export IMAGE_TAG=${GITHUB_REF_NAME}
                docker compose -f docker-compose.yml -f docker-compose.production.yml pull
                docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
                docker image prune -f
    ```
    Pre-deploy gate: phase 09 compat workflow MUST be green on the tagged commit.

12. **Bootstrap script** — `scripts/bootstrap-host.sh`: install docker + compose; create `/opt/codex-wrapper`, `/var/lib/{postgres,redis,caddy}`; install age; pull repo; run `codex login` interactively (one-time human step); render `.env` from template; run `docker compose up -d --wait`.

13. **Operations runbook** — `docs/operations-runbook.md`. Format: each section has Trigger → Symptom → Steps → Verify → Post-mortem. Cover all 9 operations + 4 error codes from tables above. Linked from each Prometheus alert annotation.

14. **Deployment guide** — `docs/deployment-guide.md`: prereqs (VM specs, DNS, S3 bucket), env walkthrough, first-time-deploy checklist, monitoring URLs (VPN/tunnel-only), rollback procedure.

15. **Host hardening (deployment-guide §)**:
    - UFW: allow 22/tcp (admin VPN), 80/tcp, 443/tcp; deny everything else.
    - Disable password ssh, key-only.
    - `unattended-upgrades` enabled.
    - Docker daemon: `userns-remap` enabled.
    - Caddy + gateway containers: `read_only: true` where possible; tmpfs for /tmp.

## Todo List
- [ ] Caddyfile.production with TLS, security headers, SSE timeouts, /v1/* allowlist
- [ ] docker-compose.production.yml overlay with restart, logging, prom/grafana/alertmanager
- [ ] postgres-backup.sh + postgres-restore.sh tested round-trip
- [ ] Redis AOF + RDB + daily backup
- [ ] tmpfs workspace volume size-capped at 10GB
- [ ] Prometheus scrape + 7 alert rules + 30d retention
- [ ] Alertmanager routes (PagerDuty + Slack)
- [ ] Grafana provisioning (datasource + 3 dashboards from phase 07)
- [ ] Log-shipper config (vector/promtail) → Loki/CloudWatch
- [ ] GH Actions deploy workflow on tag, gated on compat
- [ ] Bootstrap script for fresh VM
- [ ] Runbook with all 9 operations + 4 error codes
- [ ] Deployment guide with host hardening
- [ ] Restore exercise practiced + documented
- [ ] First-time deploy on staging VM successful
- [ ] All scripts ≤ 200 LOC

## Success Criteria
- Cold-start fresh VM: bootstrap script + first deploy completes in < 15 min, ending with HTTPS 200 on `/v1/models`.
- TLS: SSLLabs grade A or better (HSTS, TLS1.2+, no weak ciphers).
- SSE stream lasting 5 min completes successfully through Caddy (validates 1h timeout).
- Daily Postgres backup uploads encrypted dump to S3; restore script reconstructs DB into a sandbox container; `SELECT count(*) FROM api_keys` matches source.
- Alert firing path verified: kill `codex auth status` mock for 6 min → `CodexSessionUnhealthy` fires → PagerDuty webhook receives event with runbook link.
- Tag push `v0.1.0` → GH Actions builds, pushes, ssh-deploys; live within 5 min.
- Runbook §9 (session expiry recovery) completes in < 10 min when followed step-by-step.
- 30-day uptime ≥ 99.5% (excluding planned ChatGPT-session refresh windows).

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| TLS cert renewal fails (rate limit, DNS) | L | HIGH | Caddy retries automatically; alert if cert age > 80 days; manual fallback documented |
| Backup corrupted, restore fails | M | HIGH | Monthly restore exercise into sandbox; alert if backup script exits non-zero |
| Compose pull during deploy hangs | L | M | Set `--timeout 300`; rollback = previous tag |
| Single-VM = single point of failure | M | HIGH | Acknowledged for v1; multi-VM/K8s migration in v2; daily backup mitigates data loss |
| Log-shipper drops messages under load | M | M | Local Docker log driver retains last 1GB as fallback; monitor shipper lag metric |
| Alertmanager misroutes on first incident | M | M | Test alert (`alertname=Test`) fired at deploy; runbook §1 verifies receipt |
| `codex login` session expires mid-week | H | HIGH | 5-min healthcheck (phase 08) + paged alert + multi-account pool plan (v1.1) |
| Rolling restart drops 30s of streams | H | L | Acknowledged; clients retry; document SLO carve-out for deploy windows |
| GH Actions secret leak (SSH key) | L | HIGH | Use environments + required reviewers; rotate key quarterly; key restricted to deploy user only |

## Security Considerations
- TLS via Let's Encrypt; HSTS preload-eligible config.
- Public surface = `/v1/*` ONLY. `/admin/*`, `/metrics`, `/healthz`, `/readyz`, Grafana, Prometheus all internal-only (admin VPN/SSH-tunnel).
- `.env` perms 0600, owned by root; secrets injected via env, never on command line.
- Postgres backup encrypted with `age` recipient pubkey; private key held offline by ops lead.
- SSH access key-only; bastion-mode preferred.
- Docker `userns-remap` enabled for defense-in-depth against container escape.
- Edge rate-limit (1000/min/IP) is an anti-DDoS coarse bucket on top of per-key limits.
- Caddy access log ships to Loki; sensitive headers (`Authorization`) already stripped at gateway layer.
- All deploy artifacts pinned by digest in production compose (`image: ghcr.io/.../codex-wrapper@sha256:...` once stable).

## Next Steps
- Phase 11 (out of scope): multi-account ChatGPT pool with health-aware routing (brainstorm §11 deferred).
- Phase 12 (out of scope): K8s migration when concurrent users > 1k.
- Quarterly DR drill: full restore from backup into staging VM.
- Quarterly secret rotation: ADMIN_TOKEN, GHCR PAT, S3 IAM key, age key.
