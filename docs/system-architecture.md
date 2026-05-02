# System Architecture: Overview & Design

**Project:** Codex CLI OpenAI-Compatible Wrapper  
**Deployment:** Docker Compose + Caddy on single VM (internal-only via access gate)  
**Components:** FastAPI gateway + Arq worker + Postgres + Redis + Loki + Tempo + Prometheus + Grafana + Admin UI

## Table of Contents

- **[System Overview](#system-overview)** — High-level diagram
- **[Middleware Stack](#middleware-stack)** — Request processing order
- **[Data Flow Examples](#data-flow-examples)** — Chat, jobs, tool-calling
- **[Storage Model](#storage-model)** — Postgres + Redis schemas
- **[Deployment Model](#deployment-model)** — VM sizing, access gate
- **[Detailed Topics](#detailed-topics)** — See related docs for deep dives

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ Client (OpenAI SDK: Python / Node.js)                               │
├─────────────────────────────────────────────────────────────────────┤
│ TLS (SNI, mTLS optional)                                            │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Access Gate (Cloudflare Access / Tailscale / IP allowlist)          │
│ (Enforces internal-only reachability; phase-10 acceptance gate)     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Caddy 2 (Reverse Proxy + ACME TLS)                                  │
│  Port 80 (redirect HTTPS) / 443 (TLS)                               │
│  Routes:                                                            │
│    /v1/* → :8000 (FastAPI gateway, auth-required)                 │
│    /admin/* → :8000 (FastAPI admin UI, cookie-session auth)       │
│    /_internal/metrics → Prometheus scrape endpoint                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
        ┌──────────────┐         ┌──────────────┐
        │ FastAPI      │         │ Prometheus   │
        │ Gateway      │         │ (metrics)    │
        │ :8000        │         │ :9090        │
        └──────────────┘         └──────────────┘
              │
    ┌─────────┼─────────┐
    │         │         │
    ▼         ▼         ▼
  Request  Inline    Enqueue
  Handler  Runner    Job
  Stack    (SSE)     (Arq)
    │         │         │
    │         │         ▼
    │         │    ┌──────────────┐
    │         │    │ Redis Queue  │
    │         │    │ (Arq)        │
    │         │    └──────────────┘
    │         │         │
    │         │         ▼
    │         │    ┌──────────────────┐
    │         │    │ Arq Worker       │
    │         │    │ (async runner)   │
    │         │    │ :8001            │
    │         │    └──────────────────┘
    │         │         │
    │         │         ▼
    │         │    Codex CLI
    │         │    (subprocess)
    │         │
    └─────────┴──────────────┐
              │              │
              ▼              ▼
        ┌──────────────┐  ┌──────────────┐
        │ Postgres     │  │ Redis        │
        │ (durable)    │  │ (cache)      │
        │ :5432        │  │ :6379        │
        └──────────────┘  └──────────────┘
        (users, keys,   (rate-limit,
         jobs, audit)    queue, pubsub)


Observability Stack:
┌─────────────────────────────────────────────────────────────┐
│ structlog JSON → stdout (containers)                        │
├─────────────────────────────────────────────────────────────┤
│ ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │
│ │Promtail  │→ │ Loki     │  │Prometheus  │ │Tempo (OTLP)  │ │
│ │(log      │  │(logs)    │  │(metrics) │  │(traces)      │ │
│ │shipper)  │  │          │  │:9090     │  │:4317 gRPC    │ │
│ └──────────┘  └──────────┘  └──────────┘  └──────────────┘ │
│       │             │             │             │           │
│       └─────────────┴─────────────┴─────────────┘           │
│                     │                                        │
│                     ▼                                        │
│            ┌────────────────┐                               │
│            │ Grafana        │                               │
│            │ (dashboards)   │                               │
│            │ :3001 (port)   │                               │
│            └────────────────┘                               │
└─────────────────────────────────────────────────────────────┘

Backup & Disaster Recovery:
┌─────────────────────────────────────────────────────────────┐
│ Daily Cron:                                                 │
│   pg_dump → age encrypt → S3                               │
│ Quarterly:                                                  │
│   Restore drill (verify backup integrity)                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Middleware Stack (Request Order)

Middleware is applied in **outermost → innermost** order. Request flows left-to-right; response flows right-to-left.

```
    Request                                                Response
      │                                                      ▲
      ▼                                                      │
  ┌─────────────────┐                              ┌─────────────────┐
  │ RequestID       │ (Generate unique ID)         │ RequestID       │
  │ middleware      │ Add to scope["state"]        │ middleware      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────┐                              ┌─────────────────┐
  │ Observability   │ (Start timing, setup logger) │ Observability   │
  │ middleware      │ Log request start/end        │ middleware      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────┐                              ┌─────────────────┐
  │ EdgeIPLimiter   │ (Rate-limit by IP)           │ EdgeIPLimiter   │
  │ middleware      │ Reject if mesh quota exceeded│ middleware      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────┐                              ┌─────────────────┐
  │ Auth            │ (Validate bearer token)      │ Auth            │
  │ middleware      │ Lookup user, set request.user│ middleware      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────┐                              ┌─────────────────┐
  │ RateLimit       │ (RPM, TPM, concurrent)       │ RateLimit       │
  │ middleware      │ Check quotas, store headers  │ middleware      │
  │ (raw ASGI)      │ in scope["state"]            │ (raw ASGI)      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────┐                              ┌─────────────────┐
  │ UsageTracking   │ (Record API call)            │ UsageTracking   │
  │ middleware      │ Async log to audit_log (bg)  │ middleware      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────┐                              ┌─────────────────┐
  │ Timeout         │ (Hard limit per request)     │ Timeout         │
  │ middleware      │ SSE streams exempt           │ middleware      │
  └────────┬────────┘                              └────────┬────────┘
           │                                                │
           ▼                                                ▲
  ┌─────────────────────────────────────────────────────────┐
  │ Route Handler (app.py routes)                           │
  │   GET  /v1/models                                       │
  │   POST /v1/chat/completions (with tool-calling)        │
  │   POST /v1/responses                                    │
  │   POST /v1/codex/jobs                                   │
  │   GET  /v1/codex/jobs/{id}                              │
  │   DELETE /v1/codex/jobs/{id}                            │
  │   GET  /v1/codex/jobs/{id}/events                       │
  │   POST /v1/admin/api-keys                               │
  │   ... (admin routes)                                    │
  └─────────────────────────────────────────────────────────┘
```

**Key properties:**
- **RequestID** outermost: all logs include request_id automatically
- **RateLimit** raw ASGI (not BaseHTTPMiddleware): handles streaming correctly, stores headers in scope for route to use
- **Auth** before RateLimit: ensures user context available for per-user rate limits
- **Timeout** before routes: applies per-request timeout (SSE streams read timeout setting and skip hard cutoff)

---

## Data Flow Examples

### Chat Completions (Sync) — with Tool-Calling Support

```
1. POST /v1/chat/completions
   {"model": "codex", "messages": [...], "tools": [...], "tool_choice": "auto"}

2. Middleware Stack
   RequestID: req-1234567890
   Auth: lookup user by API key
   RateLimit: check RPM/TPM/concurrent (scope state)
   
3. Route Handler (sync_chat_completions)
   a. Validate request (image_url rejected; tool_calls schema validated)
   b. Build prompt from messages + tool definitions
      - format_tools_prompt() inlines full JSON schema for each tool
      - Codex learns nested structure (critical for HA execute_services)
   c. Spawn codex runner: codex exec --json "{prompt}"
   
4. Parse Response
   a. Collect all JSONL events from stdout
   b. Check if response is JSON with "tool_calls" key:
      - YES → Parse as tool calls, set finish_reason="tool_calls"
      - NO → Plain text response, set finish_reason="stop"
   
5. HTTP Response (200 OK)
   If tool_calls:
     {"choices": [{"message": {"content": null, "tool_calls": [...]}, "finish_reason": "tool_calls"}]}
   Else:
     {"choices": [{"message": {"content": "..."}, "finish_reason": "stop"}]}
```

See [System Architecture: Modules & Data Flow](system-architecture-modules.md) for detailed flows including streaming, jobs, and real-codex drift detection.

---

## Storage Model

### Postgres (Durable State)

**Tables:**
- `users` — User accounts (email, created_at)
- `api_keys` — Bearer tokens (key_hash, plan_id, status, expires_at, api_key_id for job tracking)
- `jobs` — Task history (repo_url, branch, task, status, result, error, expires_at, api_key_id FK, input_tokens, output_tokens)
- `plans` — Rate-limit tiers (rpm_quota, tpm_quota, concurrent_quota, monthly_quota)
- `audit_log` — API call logging (user_id, method, path, status, duration_ms, error)
- `usage_daily` — Daily per-user/per-key request + token aggregates (composite PK: user_id, api_key_id, period=date; indexed)
- `usage_counter` — Monthly usage per user (month, tokens_used, requests)

### Redis (Cache & Queue)

**Namespaces:**
- `rl:rpm:{user_id}:{minute_window}` — Sliding window request count (Lua script)
- `rl:tpm:{user_id}:{minute_window}` — Token count (counter, refreshed on window slide)
- `rl:concurrent:{user_id}` — Real-time concurrent request count (PEXPIRE 100ms)
- `arq:job:{job_id}` — Arq queue entries
- `job:{job_id}:events` — Pub/sub channel for job event streaming
- `cancel:{job_id}` — Cancellation flag (TTL 5 minutes)
- `codex:auth:session_hash` — Session validation cache (TTL 1 hour)

---

## Authentication Model

### Bearer Token Flow (API)

1. User creates API key: `POST /v1/admin/api-keys`
2. Server generates + hashes: `raw_key = "sk-" + random(32)`, `key_hash = argon2id(raw_key)`
3. Return raw_key once (never stored again)
4. Client request: `Authorization: Bearer sk-abc123...`
5. Gateway auth middleware: lookup, verify, set `request.user`
6. All API calls logged to `audit_log` (per-key audit trail)

### Admin UI Cookie Session Auth

- **Route:** `/admin/ui/*` (HTMX + Jinja2 + Tailwind/Chart.js CDN)
- **Auth method:** Cookie-based HMAC-SHA256 signed session (Redis-backed, 8h TTL)
- **Login:** `POST /admin/ui/login` with `ADMIN_TOKEN`
- **Cookie:** HttpOnly + SameSite=Strict, refresh on each request
- **Pages:**
  - `/admin/ui/` — Dashboard (KPI cards, sparkline charts, 5s polling)
  - `/admin/ui/keys` — API key CRUD (create, revoke, rotate, tier change)
  - `/admin/ui/tiers` — Tier RPM/TPM/concurrent/monthly editor with cache invalidation
  - `/admin/ui/jobs` — Job inspector with stderr proxy modal
  - `/admin/ui/audit` — Audit log viewer
  - `/admin/ui/users` — Per-user usage with 30-day daily usage chart

---

## Workspace & Sandbox Model

### Ephemeral Workspace Lifecycle

```
1. Job creation → mkdir /tmp/workspace-{job_id}
2. Clone repo → git clone --depth=1 -b {branch} {repo_url}
3. Run codex → codex exec --json --sandbox workspace-write "{task}"
4. Generate diff
5. Cleanup → rm -rf /tmp/workspace-{job_id}
```

### Path Safety (C6 Red-Team Fix)

Uses `os.path.realpath()` + `os.path.commonpath()` to prevent `../` escape:
```python
root_real = os.path.realpath(workspace_root)
path_real = os.path.realpath(requested_path)
common = os.path.commonpath([root_real, path_real])
if common != root_real:
    raise InvalidWorkspacePath(...)
```

### Sandbox Enforcement

Codex `--sandbox workspace-write`:
- **Linux ≥5.13:** Landlock
- **Linux <5.13:** seccomp
- **macOS:** Seatbelt

Prevents: network, system calls, file access outside workspace.

---

## Observability: Logs + Metrics + Traces

### Structured Logging (structlog → Loki)

All logs emit JSON to stdout with fields: `request_id`, `service`, `level`, `event`, `ts`. Promtail ships to Loki. Searchable by labels and JSON content.

### Metrics (Prometheus)

16 instruments:
- `codex_wrapper_request_duration_seconds` (histogram: p50/p95/p99)
- `codex_wrapper_request_errors_total` (counter by error_type)
- `codex_wrapper_rate_limit_headroom_tokens` (gauge per user)
- `codex_wrapper_job_queue_depth` (gauge: pending jobs)
- 12 more (see codebase-summary.md for full list)

Scrape interval: 15s, retention: 15 days.

### Traces (OpenTelemetry → Tempo)

Span hierarchy with trace IDs, parent IDs, attributes (user_id, job_id, status, duration_ms). Grafana Tempo UI for waterfall visualization.

---

## Deployment Model

### Single VM (Docker Compose)

**Host mounts:**
- `/var/lib/codex-wrapper/postgres` — Postgres data
- `/var/lib/codex-wrapper/redis` — Redis data
- `~/.codex/` — ChatGPT auth (RO)
- `/tmp` — tmpfs for workspaces

**Containers:**
- `gateway` (:8000), `worker` (no port), `postgres` (:5432, internal), `redis` (:6379, internal)
- `caddy` (:80, :443 TLS), `otel-collector` (:4317), `prometheus` (:9090)
- `loki` (:3100), `tempo` (:4317), `grafana` (:3000)

**Access gate (external):** Cloudflare Access / Tailscale / IP allowlist enforces internal-only reachability.

---

## Detailed Topics

For in-depth coverage, see:

| Topic | Document |
|-------|----------|
| Tool-calling synthesis, HA EOC compatibility, detailed data flows | [System Architecture: Modules & Data Flow](system-architecture-modules.md) |
| Rate-limit four-tier model, Lua scripts, fairness | [System Architecture: Modules & Data Flow](system-architecture-modules.md) |
| Secret management, SSRF defense, workspace isolation | [System Architecture: Modules & Data Flow](system-architecture-modules.md) |
| Backup & disaster recovery, restore procedures | [System Architecture: Modules & Data Flow](system-architecture-modules.md) |
| Alerting rules, Prometheus configuration | [System Architecture: Modules & Data Flow](system-architecture-modules.md) |

---

**Last Updated:** 2026-05-02 (admin UI, daily usage tracking, Prometheus + Grafana integration, Phase 07-10 complete)
