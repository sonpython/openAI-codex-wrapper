# Phase 08: Hardening

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§7 risks: long-running DoS, secret leak, subprocess zombie, account ban)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (§5 auth disk state, §8 timeout/cancel semantics)
- Phase 02: phase-02-codex-runner.md (subprocess basics — extended here)
- Phase 05: phase-05-jobs-and-arq.md (cancel API, repo URL validation)
- Phase 07: phase-07-observability.md (metrics + logs feed alert rules here)
- Project rules: ../../.claude/rules/development-rules.md

## Overview
- Priority: high
- Status: pending
- Effort: M
- Description: Production safety net. Hardens timeouts, cancellation, workspace cleanup, secret rotation, audit log coverage, input validation, and Codex session monitoring. Phase 02 + 05 had basic versions; this phase makes them production-grade with cleanup paths, alerting, and admin-only mutation surfaces.

## Red Team Resolutions
Addresses: **C8** (audit_log fire-and-forget pool exhaustion + task GC — references phase-01 master pattern), **C10** (SSRF defense incomplete — disable redirect-follow + transport-layer DNS-private-IP rejection), **MM6** (Codex stderr postmortem retention — full stderr archived for failed jobs).

Phase-01 owns the canonical `_BG_TASKS` background-task tracking pattern + dedicated bg pool; this phase reuses it for `audit_log.emit` and any cron writes.

## Key Insights
- Codex itself does NOT emit a guaranteed final event on SIGTERM in non-interactive mode (researcher-01 §8 unresolved). Wrapper must own its own cancel state machine — don't trust subprocess to clean up.
- Workspace leaks compound: a single uncaught exception in the worker leaves a `/workspaces/{job_id}` dir that grows monotonically. Periodic janitor is mandatory, not optional.
- Audit log MUST be async fire-and-forget — synchronous DB write per request adds latency + creates a new failure mode where audit-DB downtime breaks the API path.
- Admin endpoints under `/admin/*` are a SEPARATE auth surface from `/v1/*`. Use a different bearer (`ADMIN_TOKEN` env, constant-time compare). Never share api_keys schema.
- Secret rotation 24h grace window is a deliberate trade-off: convenience for clients vs window where compromised key still works. Document it; make it tier-tunable later if needed.
- Codex session expiry is the #1 incident risk (brainstorm §7). 5min healthcheck + readiness-probe-fails-on-expiry sheds load gracefully (Caddy returns 503) instead of returning corrupted streams.

## Requirements

### Functional
- Per-route hard timeouts enforced (table below). On exceed → cancel codex subprocess, return 504 Gateway Timeout.
- Client-disconnect detection: when `request.is_disconnected()` returns True, codex subprocess is sent SIGTERM within 1s.
- SIGTERM → 5s grace → SIGKILL escalation with workspace cleanup in finally block.
- Workspace janitor cron: every 10 min, scan `/workspaces/*` mtime > 1h AND not in active job table → recursive remove.
- Admin API: `POST /admin/api-keys`, `GET /admin/api-keys`, `POST /admin/api-keys/{id}/rotate`, `DELETE /admin/api-keys/{id}`. All audit-logged.
- Rotated key has 24h grace where both old and new authenticate; old `revoked_at` set to `now + 24h`.
- Audit log row written for every `/v1/*` request and every `/admin/*` operation.
- Prompt size limit: 256k chars per request; reject 413 if over.
- Repo URL HEAD-check before enqueue (5s timeout, 2 retries, 5min cache).
- Codex session healthcheck every 5 min: on failure, log + alert webhook + flip readiness probe to 503.

### Non-Functional
- Audit-log path adds < 5ms p99 (async write).
- Janitor execution < 30s for 10k workspace dirs.
- Admin endpoints rate-limited 10 RPM per IP (independent of api_key tiers).
- All Python files ≤ 200 LOC.
- Cleanup paths covered by integration tests (kill mid-stream, kill mid-job, OOM, panic).

## Architecture

```
request lifecycle (chat/responses):
  ┌──────────────────────────────────────────────────────────────┐
  │ middleware/timeout.py:                                       │
  │   asyncio.wait_for(handler, timeout=route_timeout)           │
  │   on TimeoutError → cancel codex subprocess → 504             │
  └──────────────────────────────────────────────────────────────┘
                  │
                  ▼
  handler:
    runner.run() inside try/finally:
      try:
        async for event in runner.stream():
          if request.is_disconnected(): runner.cancel(); break
          yield event
      finally:
        runner.cancel()  # idempotent; ensures subprocess + workspace cleaned
        await audit_log.emit(...)  # fire-and-forget, do not await long

worker lifecycle (jobs):
  arq job handler:
    async with workspace_ctx(job_id) as ws:  # tmpdir + atexit hook
      try:
        await git_clone(ws, repo_url)
        await runner.run(ws)
      finally:
        await workspace_ctx_cleanup(ws)
        await audit_log.emit(...)

cron schedules (arq scheduled tasks):
  janitor.cleanup_stale_workspaces()        every 10 min
  auth_session.healthcheck()                every  5 min
  audit_log.purge_old(retention_days=90)    daily 03:00 UTC

admin surface:
  /admin/api-keys/*   ←  middleware/admin_auth.py (ADMIN_TOKEN constant-time)
                        rate-limit 10/min per IP
                        audit-logged with admin: true

readiness probe:
  /readyz checks:
    - DB ping
    - Redis ping
    - codex_session_healthy (from auth_session.healthcheck cache)
  → returns 503 if any fail → Caddy stops routing → graceful shed
```

### Per-route timeouts

| Route | Default timeout | Tier-tunable | Notes |
|---|---|---|---|
| `POST /v1/chat/completions` (sync) | 120s | yes | Codex one-shot |
| `POST /v1/chat/completions` (stream) | 120s wall | yes | per-chunk no-progress timer 30s |
| `POST /v1/responses` (sync) | 120s | yes | same as chat |
| `POST /v1/responses` (stream) | 120s wall | yes | same |
| `POST /v1/codex/jobs` (enqueue) | 5s | no | DB+Redis only |
| Job execution (worker) | 900s | yes | hard wall on subprocess |
| `GET /v1/models` | 5s | no | static |
| `GET /v1/codex/jobs/{id}` | 5s | no | DB only |

### Audit log columns (extends phase 01 stub)

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial | pk |
| `created_at` | timestamptz | indexed |
| `request_id` | text | from middleware |
| `api_key_id` | uuid? | null for admin |
| `user_id` | uuid? | from key lookup |
| `admin` | bool | true for /admin/* ops |
| `route` | text | FastAPI route template |
| `method` | text | HTTP verb |
| `status_code` | int | response code |
| `duration_ms` | int | full request lifetime |
| `codex_cmd` | text[]? | argv joined (no secrets) |
| `prompt_hash` | text? | sha256 first prompt; null if no prompt |
| `input_tokens` | int? | from `turn.completed` usage |
| `output_tokens` | int? | from `turn.completed` usage |
| `codex_exit_code` | int? | runner |
| `error_class` | text? | exception class on failure |
| `target_id` | uuid? | for admin ops (api_key_id rotated) |
| `action` | text? | for admin ops (`create`, `rotate`, `revoke`) |

`AUDIT_LOG_PROMPT=false` (default): NEVER log raw prompt; only sha256 hash. Set `true` only in dev for debugging.

## Related Code Files

### To create
- `src/gateway/middleware/timeout.py` (≤ 120 LOC) — `asyncio.wait_for` wrapper with route-config lookup.
- `src/gateway/middleware/admin_auth.py` (≤ 80 LOC) — bearer compare with `ADMIN_TOKEN`, constant-time, rate-limit.
- `src/gateway/admin/__init__.py`
- `src/gateway/admin/api_keys_routes.py` (≤ 200 LOC) — CRUD + rotate handlers.
- `src/db/crud/audit_log.py` (≤ 150 LOC) — `emit()` async fire-and-forget; `purge_old()`.
- `src/db/crud/api_keys_admin.py` (≤ 180 LOC) — create/list/rotate/revoke ops; audit-log integration.
- `src/workers/janitor.py` (≤ 120 LOC) — workspace scan + remove; arq scheduled task.
- `src/codex/auth_session.py` (extend phase 02; add webhook alerts; ≤ 150 LOC total).
- `src/observability/alert_webhooks.py` (≤ 80 LOC) — Slack/email POST helper.
- `src/gateway/ssrf_transport.py` (≤ 120 LOC) — `SSRFGuardedTransport` (subclass `httpx.AsyncHTTPTransport`); aiodns resolution + private-IP rejection.
- `src/codex/stderr_archive.py` (≤ 120 LOC) — write/read stderr blobs to bucket (boto3 / aioboto3).
- `src/gateway/admin/codex_stderr_routes.py` (≤ 80 LOC) — `GET /admin/codex/jobs/{id}/stderr`.
- `tests/unit/test_timeout_middleware.py`
- `tests/unit/test_admin_auth.py`
- `tests/unit/test_admin_api_keys.py`
- `tests/unit/test_audit_log.py`
- `tests/unit/test_janitor.py`
- `tests/unit/test_repo_url_headcheck.py`
- `tests/integration/test_client_disconnect.py` — kills client mid-stream, asserts subprocess reaped < 5s.
- `tests/integration/test_session_expiry.py` — mocks codex auth status fail, asserts /readyz → 503.

### To modify
- `src/codex/runner.py` (phase 02) — add `cancel()` idempotent method; ensure SIGTERM→5s→SIGKILL; finally-block workspace cleanup; client-disconnect polling.
- `src/codex/workspace.py` (phase 02) — add `atexit.register` for crash recovery; add `mtime` helper for janitor.
- `src/gateway/routes/chat.py`, `responses.py`, `jobs.py` — wire timeout middleware route-config; add try/finally with audit emit.
- `src/gateway/routes/jobs.py` (phase 05) — add HEAD check before enqueue; add prompt size validate.
- `src/gateway/health.py` (phase 00) — `/readyz` reads `auth_session.healthy_cached()`.
- `src/db/models.py` — extend `audit_log` table per columns above.
- `src/settings.py` — add `ADMIN_TOKEN`, `AUDIT_LOG_PROMPT`, `AUDIT_RETENTION_DAYS`, `JOB_TIMEOUT_SECONDS` (existing), `JOB_CANCEL_GRACE_SECONDS` (existing), `WEBHOOK_ALERT_URL`, `WEBHOOK_ALERT_KIND` (`slack`/`http`), `PROMPT_MAX_CHARS=262144`, `REPO_HEAD_TIMEOUT=5`, `REPO_HEAD_CACHE_SECONDS=300`, `STDERR_ARCHIVE_BUCKET`, `STDERR_ARCHIVE_PREFIX`, `STDERR_RETENTION_DAYS=14`.
- `src/workers/arq_worker.py` (phase 05) — register janitor cron + auth_session healthcheck cron + audit purge cron.
- `src/db/migrations/versions/<new>.py` — alembic revision adding columns to `audit_log`.

### To delete
(none)

## Implementation Steps

1. **Audit log model + migration** — Extend `audit_log` table with new columns. Generate alembic revision. Backfill: existing rows get `admin=false`. Add index on `(api_key_id, created_at desc)` for tail-by-key queries.
2. **Audit emit function** — `audit_log.emit(**fields) -> None`:
   - Use the canonical `_BG_TASKS: set[asyncio.Task]` background-task pattern from phase-01 (the master implementation): `task = asyncio.create_task(_persist(fields)); _BG_TASKS.add(task); task.add_done_callback(_BG_TASKS.discard)`. Bare `asyncio.create_task` is BANNED — leads to "Task was destroyed but it is pending" GC warnings + silent audit drop under memory pressure.
   - `_persist` opens session from the **dedicated background async session factory** (size 2-3, `pool_timeout=0.5`) — NOT the request pool. Defined in phase-01. On pool acquire timeout: log WARN + drop; do NOT block request path.
   - Swallows all exceptions (log only, never raise — never break request path).
   - Optional v1.1: replace fan-out with a single `asyncio.Queue` consumer task to bound concurrent DB writes to 1; safer under load. Documented for v1.1, not required for v1.
3. **Timeout middleware** — Read route-template → timeout from config dict (settings-driven). Wrap downstream call in `asyncio.wait_for`. On `TimeoutError`: log + bump `http_request_timeout_total{route}` + return 504 with OpenAI-shaped error JSON `{"error":{"type":"timeout","message":"...","code":"timeout"}}`.
4. **Subprocess cancellation hardening** — In `runner.cancel()`:
   1. If process not running → return.
   2. `process.send_signal(SIGTERM)`.
   3. `await asyncio.wait_for(process.wait(), timeout=settings.JOB_CANCEL_GRACE_SECONDS)`.
   4. On timeout → `process.kill()` (SIGKILL), `await process.wait()`.
   5. Cleanup workspace (rm -rf safe-validated path).
   6. Bump `codex_subprocess_exit_code_total{code=cancelled}`.
   Idempotent: track `_cancelled` flag; second call is no-op.
5. **Client-disconnect detection** — In streaming handlers, every yield iteration check `await request.is_disconnected()`. If True, log `client.disconnected`, call `runner.cancel()`, break loop. Test (integration): pytest `httpx.AsyncClient` with `timeout=0.5`; assert `codex_active_subprocess` returns to 0 within 5s.
6. **Workspace cleanup hardening** — Three layers:
   - `runner.run()` try/finally always calls cleanup.
   - `atexit.register(_emergency_cleanup)` in worker startup — clears all `/workspaces/*` on shutdown.
   - Janitor cron (step 7).
7. **Janitor cron** — `workers/janitor.py`:
   ```
   async def cleanup_stale_workspaces(ctx):
     now = time.time()
     active_ids = {r.id for r in await db.fetch_active_jobs()}
     for path in os.scandir(settings.WORKSPACE_ROOT):
       if path.name in active_ids: continue
       if (now - path.stat().st_mtime) < 3600: continue
       safe_path = validate_path_inside(settings.WORKSPACE_ROOT, path.path)
       shutil.rmtree(safe_path, ignore_errors=True)
       log.info("janitor.cleaned", path=path.name)
   ```
   Register with arq cron `every 10 min`. Bump `workspace_disk_bytes` gauge after cleanup.
8. **Admin token middleware** — `admin_auth.py`: extract `Authorization: Bearer <token>`; `hmac.compare_digest(token, settings.ADMIN_TOKEN)`. Reject 401 on miss. Apply ONLY to `/admin/*` routes via FastAPI dependency. Rate-limit 10/min per IP via existing redis sliding window keyed by IP.
9. **Admin API handlers**:
   - `POST /admin/api-keys` body=`{user_id, tier, name?}` → returns `{id, key_prefix, key_plaintext}` ONCE.
   - `GET /admin/api-keys?user_id=&tier=` → list `{id, prefix, tier, last_used_at, revoked_at, created_at}`.
   - `POST /admin/api-keys/{id}/rotate` → generate new key; mark old `revoked_at = now + 24h`; create new row linking `replaces_id = old.id`. Return new `{id, key_prefix, key_plaintext}`.
   - `DELETE /admin/api-keys/{id}` → set `revoked_at = now`.
   - All four call `audit_log.emit(admin=True, action=..., target_id=...)`.
10. **Auth lookup respects rotation grace** — In phase-01 auth middleware, accept key if `revoked_at IS NULL OR revoked_at > now()`. (Already supports it if column nullable.) Test: rotate key; both old + new authenticate for 24h.
11. **Input validation hardening**:
    - Prompt size: in chat/responses/jobs handlers, sum char length of all messages.content; reject 413 OpenAI-shaped if > `PROMPT_MAX_CHARS`.
    - **Repo URL HEAD-check (SSRF-hardened)**:
      - Regex (allowlist): `^https://(github\.com|gitlab\.com)/[\w.-]+/[\w.-]+(\.git)?$` (already in phase 05).
      - **`follow_redirects=False`**: a 3xx from github/gitlab to off-allowlist or attacker-controlled host is a tampering signal — reject 422 with actionable error `{"error":{"type":"invalid_request","message":"repo URL redirected unexpectedly; provide canonical URL","param":"repo_url"}}`. Note: GitHub legitimately redirects renamed repos; users must update their URL — this is the correct behavior for SSRF safety.
      - **Transport-layer DNS-private-IP rejection** (defense-in-depth — `httpx` does NOT block private-IP resolution by default):
        ```python
        # SSRFGuardedTransport: subclass httpx.AsyncHTTPTransport
        # Pre-connect: resolve hostname via aiodns; for EVERY answer (A + AAAA), reject if private.
        PRIVATE_V4 = [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("169.254.0.0/16"),    # link-local
            ipaddress.ip_network("100.64.0.0/10"),     # CGNAT (RFC6598)
            ipaddress.ip_network("0.0.0.0/8"),
        ]
        PRIVATE_V6 = [
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"),          # ULA
            ipaddress.ip_network("fe80::/10"),         # link-local
            ipaddress.ip_network("::/128"),
        ]
        # Use aiodns.DNSResolver().query(host, 'A') + ('AAAA'); block if any answer in PRIVATE_*.
        # If host is already an IP literal (not hostname), check directly.
        ```
        Plug into `httpx.AsyncClient(transport=SSRFGuardedTransport(...))`.
      - HEAD timeout 5s, 2 retries, exponential backoff.
      - Cache result (URL → bool) in Redis with TTL 300s.
      - On any failure (DNS-private, redirect, non-200, timeout): reject 422 with stable error code; do NOT leak resolution details to client.
      - **Test fixtures** (compose.test.yml): use Docker `--add-host malicious-public.test:127.0.0.1` (or `--resolve` via httpx) so a "public-looking" hostname resolves to RFC1918/loopback in tests; assert HEAD-check rejects.
    - Branch name: `^[A-Za-z0-9._/-]{1,200}$`; reject 422 on miss.
    - Path validation: ALL filesystem ops via `validate_path_inside(root, path)` helper from phase 02.
12. **Codex session monitoring** — Extend `auth_session.py`:
    - `async def healthcheck() -> bool`: run `codex auth status` (subprocess, 10s timeout); return exit==0.
    - Cache result in Redis key `codex:session:healthy` with TTL 300s + last-checked timestamp.
    - On transition healthy→unhealthy: log error + POST webhook + bump `codex_session_unhealthy=1` gauge.
    - `/readyz` reads cached value; returns 503 if unhealthy.
    - Arq cron every 5 min calls `healthcheck()`.
13. **Alert webhook helper** — `observability/alert_webhooks.py`: `async def send_alert(severity, message, fields)`. If `WEBHOOK_ALERT_URL` set: POST JSON `{text, severity, fields}`. If `WEBHOOK_ALERT_KIND=slack`: shape as `{"text":...}` Slack block. Best-effort; swallow errors.
14. **Audit retention purge** — Daily cron `audit_log.purge_old()`: `DELETE FROM audit_log WHERE created_at < now() - interval '<retention> days'`. Default 90; env-tunable.
15. **Codex stderr postmortem retention** (MM6):
    - Phase 02 captures last 64 KiB of stderr in an in-process ring buffer; current behavior: log WARN level on Codex non-zero exit then discard. On-call cannot diagnose "codex crashed at 3am, why?".
    - For terminal-failure jobs (`exit_code != 0` OR `error_class is not null`): persist the FULL ring-buffer contents (capped 64 KiB) to object storage. Storage backend:
      - dev: bind-mount `/var/codex-stderr/{job_id}.log` (volume in compose.dev.yml).
      - prod: S3 / Backblaze B2 keyed `codex-stderr/{YYYY-MM}/{job_id}.log`. Use phase-10 backup target bucket; same credentials.
    - Retention: 14 days (lifecycle rule on bucket; cron in dev).
    - Admin-only retrieval: `GET /admin/codex/jobs/{id}/stderr` — streams the file body, requires `ADMIN_TOKEN`, audit-logged with `action=stderr_retrieve`. Reject 404 if no stderr captured (job succeeded or not yet failed).
    - Settings: `STDERR_ARCHIVE_BUCKET`, `STDERR_ARCHIVE_PREFIX`, `STDERR_RETENTION_DAYS=14`.
    - Test: trigger a Codex failure (mock-codex returns exit 1 with stderr "boom"), assert archive object exists, `GET /admin/codex/jobs/{id}/stderr` returns the body.
16. **Tests** (selection):
    - `test_timeout_middleware`: handler sleeping 200s with 1s timeout → 504 returned + log emitted.
    - `test_client_disconnect`: integration fixture spawns gateway+worker+mock-codex (phase 09 fixture); client opens stream, drops connection after 1 chunk; assert `codex_active_subprocess` gauge = 0 within 5s.
    - `test_admin_auth`: bad token → 401; good token → 200; rate-limit kicks at 11th request.
    - `test_admin_rotate`: create key, use it (200), rotate, both old and new return 200, fast-forward 24h (mock now), old returns 401.
    - `test_audit_log_emit`: emit fields, query DB, assert row inserted; assert NO raw prompt text in row when `AUDIT_LOG_PROMPT=false`.
    - `test_janitor`: create stale + fresh + active workspaces; run janitor; assert only stale removed.
    - `test_repo_url_headcheck`: 200 url cached → second call no HTTP; 404 url → 422; 5xx → 422 with retry counted.
    - `test_repo_url_ssrf`: hostname resolving to 127.0.0.1 (via `--add-host` or transport hook injection) → 422 with `repo_url` invalid_request; 3xx-redirect response → 422; assertion that no actual HTTP body fetched against the private target.
    - `test_session_expiry`: monkeypatch `healthcheck()` False → `/readyz` returns 503; webhook called once.
    - `test_codex_stderr_archive`: mock-codex exits 1 with stderr "boom"; assert `STDERR_ARCHIVE_BUCKET` object exists, `GET /admin/codex/jobs/{id}/stderr` (with admin token) returns "boom"; without admin token returns 401.

## Todo List
- [ ] Audit log table extended + migration applied
- [ ] `audit_log.emit` uses phase-01 `_BG_TASKS` pattern + dedicated bg pool (not request pool)
- [ ] Per-route timeout middleware enforced; 504 on timeout
- [ ] Subprocess SIGTERM→5s→SIGKILL with workspace cleanup
- [ ] Client-disconnect cancels subprocess < 5s
- [ ] Janitor cron registered, removes stale workspaces
- [ ] Admin token middleware (constant-time + IP rate-limit)
- [ ] Admin API: create/list/rotate/revoke api_keys
- [ ] Rotation 24h grace window verified
- [ ] Prompt size limit 256k chars enforced
- [ ] Repo URL HEAD-check: `follow_redirects=False`, SSRFGuardedTransport rejects private IPv4/IPv6 via aiodns
- [ ] SSRF integration test with `--add-host` resolving "public" hostname to RFC1918 → 422
- [ ] Branch name allowlist regex
- [ ] Codex session healthcheck cron + readiness flip
- [ ] Alert webhook helper + integration with session monitor
- [ ] Audit retention purge cron
- [ ] Codex stderr archive: failed-job stderr → S3/B2 (prod) or volume (dev), 14d retention
- [ ] `GET /admin/codex/jobs/{id}/stderr` admin-only retrieval endpoint
- [ ] All tests pass; integration covers disconnect + session expiry + SSRF + stderr archive
- [ ] No file > 200 LOC

## Success Criteria
- `curl -X POST /v1/chat/completions` with prompt > 256k → 413 with OpenAI-shaped error.
- `curl --max-time 1` against streaming endpoint → server logs `client.disconnected` within 1s; `codex_active_subprocess` gauge returns 0 within 5s (Prom scrape).
- Worker hard timeout: `/v1/codex/jobs` with 901s job → status flips to `failed` with `error_class=timeout`; subprocess reaped; workspace cleaned.
- Admin: rotate key → both old+new authenticate; after 24h (mock clock) old returns 401.
- Admin endpoints: 11 requests/min from same IP → 11th returns 429.
- `codex auth status` mock failure → `/readyz` returns 503; webhook fired once; Grafana panel shows `codex_session_unhealthy=1`.
- Janitor: 100 stale dirs in `/workspaces/`, cron run < 30s, all removed; active job dirs untouched.
- `pytest tests/integration/test_client_disconnect.py` and `test_session_expiry.py` pass in CI.
- Audit log: every `/v1/*` request produces exactly one row with redacted fields and `prompt_hash` populated, NOT raw prompt.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cleanup race: cancel + concurrent access to workspace | M | M | Cancel sets `_state=cancelled` flag; cleanup is idempotent + uses validated abs path |
| Admin token leaked → key takeover | L | HIGH | Rate-limit + audit log + rotation procedure documented in runbook (phase 10) |
| 24h rotation window misused (compromised key still valid) | M | M | Document trade-off; allow `?immediate=true` flag for emergency rotation (skips grace) |
| Audit DB outage breaks request path | M | HIGH | Fire-and-forget; swallow exceptions; circuit-break to NOOP after N failures + alert |
| Janitor removes active workspace (race with new job) | L | HIGH | Active-jobs query is authoritative; mtime > 1h cutoff is buffer; jobs touch dir frequently |
| Session expiry storms cause flapping ready/not-ready | M | M | Cache 300s + hysteresis (require 2 consecutive failures before flip) |
| HEAD check against attacker-controlled URL → SSRF | M | HIGH | Strict allowlist regex (only github.com/gitlab.com); `follow_redirects=False`; SSRFGuardedTransport resolves DNS via aiodns + rejects RFC1918 + RFC6598 + 169.254 + 127.0.0.0/8 + 0.0.0.0/8 + IPv6 ULA/link-local/loopback BEFORE connect; integration test with `--add-host` poisoning |
| Audit_log emit pool exhaustion under load | M | HIGH | Dedicated bg session factory (size 2-3, pool_timeout=0.5); request path NEVER blocks on audit; `_BG_TASKS` set tracks tasks (no GC) |
| Codex stderr lost on crash → on-call cannot diagnose | H | M | Failed-job stderr archived to S3/B2 with 14d retention; admin-only retrieval endpoint |
| Prompt char-count slow on large body | L | L | Use `len(s)` not `regex.findall`; FastAPI body limit already capped upstream |

## Security Considerations
- `ADMIN_TOKEN` from env only; never in DB or logs (covered by phase 07 redaction).
- Audit log NEVER stores raw prompt by default (`AUDIT_LOG_PROMPT=false`).
- HEAD-check restricted to allowlisted hosts; `follow_redirects=False`; custom `SSRFGuardedTransport` resolves DNS via aiodns and rejects all private IPv4/IPv6 ranges (RFC1918 10/8, 172.16/12, 192.168/16, RFC6598 100.64/10, 127/8, 169.254/16, 0/8, IPv6 ::1, fc00::/7, fe80::/10, ::) BEFORE TCP connect. Integration tests verify rejection by mapping public-looking hostnames to private IPs.
- Codex stderr archive bucket: server-side encryption enabled; lifecycle rule = 14-day expiry; admin-only read access via signed URL or backend-proxied endpoint.
- Admin endpoints separate auth surface from `/v1/*`; no shared secrets.
- Cleanup paths always run under `validate_path_inside(WORKSPACE_ROOT, path)` — no traversal.
- Webhook URL validated as https + non-private (defense against admin-token-leak → exfil).
- Constant-time admin token compare (`hmac.compare_digest`).

## Next Steps
- Phase 09 SDK compat tests use admin endpoint to provision test keys.
- Phase 10 deploys alert rules consuming `codex_session_unhealthy`, `arq_queue_depth`, `workspace_disk_bytes`, `http_request_timeout_total`. Runbook documents rotation procedure + drain steps.
