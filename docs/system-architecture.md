# System Architecture

**Project:** Codex CLI OpenAI-Compatible Wrapper  
**Deployment:** Docker Compose + Caddy on single VM (internal-only via access gate)  
**Components:** FastAPI gateway + Arq worker + Postgres + Redis + Loki + Tempo + Prometheus

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
│    /v1/* → :8000 (FastAPI gateway)                                 │
│    /_internal/metrics → :9090 (Prometheus scrape)                  │
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
│            │ :3000          │                               │
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
  │   POST /v1/chat/completions                             │
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

## Data Flow: Chat Completions (Sync)

```
1. Client Request
   POST /v1/chat/completions
   Authorization: Bearer sk-abc123...
   Content-Type: application/json
   {"model": "codex", "messages": [...]}

2. Middleware Stack
   RequestID: req-1234567890
   Auth: lookup user by API key
   RateLimit: check RPM/TPM/concurrent (scope state)
   
3. Route Handler (sync_chat_completions)
   a. Parse + validate ChatCompletionsRequest
   b. Create workspace: /tmp/workspace-{chat_id}
   c. Build Codex prompt from SDK messages
   d. Spawn codex runner subprocess (no streaming)
   
4. Codex Runner
   codex exec --json \
     --ephemeral \
     --sandbox workspace-write \
     --cd /tmp/workspace-{chat_id} \
     --skip-git-repo-check \
     "{prompt_text}"
   
5. Collect JSONL Events
   Parse all events from stdout:
     {"type": "input", "content": "..."}
     {"type": "output", "content": "token", "finish_reason": null}
     {"type": "output", "content": "", "finish_reason": "stop"}
   
6. Aggregate Response
   - Combine output tokens
   - Estimate usage (tokens)
   - Build OpenAI ChatCompletionResponse
   
7. Cleanup
   - Delete workspace (/tmp/workspace-{chat_id})
   - Close runner
   
8. HTTP Response (200 OK)
   {
     "id": "chatcmpl-abc123",
     "object": "chat.completion",
     "created": 1714219000,
     "model": "codex",
     "choices": [{
       "message": {"role": "assistant", "content": "..."},
       "finish_reason": "stop",
       "index": 0
     }],
     "usage": {
       "prompt_tokens": 50,
       "completion_tokens": 42,
       "total_tokens": 92
     }
   }
   
   Headers:
   X-RateLimit-Limit-Requests: 3600
   X-RateLimit-Remaining-Requests: 3599
   X-RateLimit-Limit-Tokens: 90000
   X-RateLimit-Remaining-Tokens: 89500
```

---

## Data Flow: Chat Completions (Streaming)

```
1. Client Request (same as sync)
   POST /v1/chat/completions
   "stream": true
   
2. Middleware Stack (same as sync)
   
3. Route Handler (stream_chat_completions)
   a. Parse + validate request
   b. Create workspace
   c. Build Codex prompt
   d. Return StreamingResponse with generator
   
4. Generator Loop (async)
   async for event in codex_runner.stream():
     if event.type == "output":
       yield f"data: {ChatCompletionChunk(...)}\n\n"
   
5. Keepalive Helper
   - Every 15s with no output: emit ": keepalive\n\n" (SSE comment)
   - Prevents CDN/Caddy from closing idle connections
   
6. HTTP Response (200 OK, text/event-stream)
   data: {"id":"chatcmpl-abc","choices":[{"delta":{"content":"Hello"},...}]}
   data: {"id":"chatcmpl-abc","choices":[{"delta":{"content":" world"},...}]}
   ...
   data: {"id":"chatcmpl-abc","choices":[{"finish_reason":"stop",...}]}
   
   Headers:
   Content-Type: text/event-stream
   Cache-Control: no-cache
   Connection: keep-alive
   X-RateLimit-*: (from scope state)
```

---

## Data Flow: Codex Jobs (Async)

```
1. Client Request (enqueue)
   POST /v1/codex/jobs
   {
     "repo_url": "https://github.com/user/repo",
     "branch": "main",
     "task": "Fix the null pointer bug in src/main.py",
     "mode": "no-context|codebase|test"
   }
   
2. Middleware Stack
   (same: auth, rate-limit, etc.)
   
3. Route Handler (enqueue_job)
   a. Validate repo_url (SSRF guard)
   b. Create Job record in Postgres
     job_id = uuid4()
     status = "queued"
   c. Enqueue task to Redis/Arq
     queue.enqueue(run_codex_job, job_id=job_id)
   d. Return JobResponse (status, job_id)
   
4. HTTP Response (202 Accepted)
   {
     "id": "job-abc123",
     "status": "queued",
     "created_at": "2026-04-27T12:00:00Z",
     "repo_url": "https://github.com/user/repo",
     "branch": "main",
     "task": "..."
   }
   
5. Arq Worker Dequeues
   a. Create workspace: /tmp/workspace-{job_id}
   b. Clone repo: git clone --depth=1 -b main {repo_url}
   c. Run codex: codex exec --json "{task}"
   d. Generate diff
   e. Update Job record: status = "completed", result = {...}
   f. Publish events to Redis pub/sub: job:{job_id}:events
   
6. Client Polls or Subscribes
   
   Poll:
   GET /v1/codex/jobs/{job_id}
   (returns current job status + summary)
   
   Subscribe (SSE):
   GET /v1/codex/jobs/{job_id}/events?stream=true
   data: {"type": "job.started", "timestamp": "..."}
   data: {"type": "job.cloned", "timestamp": "..."}
   data: {"type": "job.running", "timestamp": "..."}
   data: {"type": "job.completed", "result": {...}}
```

---

## Storage Model

### Postgres (Durable State)

```sql
-- Users & Authentication
users (
  id UUID PRIMARY KEY,
  email VARCHAR(255) UNIQUE,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
)

api_keys (
  id UUID PRIMARY KEY,
  user_id UUID FK (RESTRICT),
  key_hash VARCHAR(255) NOT NULL,  -- argon2id
  key_prefix VARCHAR(20) UNIQUE,   -- shown once at creation
  plan_id UUID FK (RESTRICT),      -- rate-limit tier
  status ENUM ('active', 'rotated', 'revoked'),
  created_at TIMESTAMP DEFAULT now(),
  last_used_at TIMESTAMP,
  expires_at TIMESTAMP
)

-- Jobs
jobs (
  id UUID PRIMARY KEY,
  user_id UUID FK (RESTRICT),
  repo_url VARCHAR(2048),
  branch VARCHAR(255),
  task TEXT,
  mode ENUM ('no-context', 'codebase', 'test'),
  status ENUM ('queued', 'running', 'completed', 'failed', 'cancelled'),
  result JSONB,                    -- diff, summary
  error TEXT,
  created_at TIMESTAMP DEFAULT now(),
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  expires_at TIMESTAMP + 24h
)

-- Rate-Limit Plans
plans (
  id UUID PRIMARY KEY,
  name VARCHAR(255),               -- 'free', 'pro', 'enterprise'
  rpm_quota INT,
  tpm_quota INT,
  concurrent_quota INT,
  monthly_quota INT
)

-- Audit Log (best-effort, async via bg_session)
audit_log (
  id UUID PRIMARY KEY,
  user_id UUID FK (RESTRICT),
  api_key_id UUID FK (RESTRICT),
  method VARCHAR(10),              -- 'GET', 'POST', 'DELETE'
  path VARCHAR(2048),
  status INT,
  response_size_bytes INT,
  duration_ms INT,
  error TEXT,
  created_at TIMESTAMP DEFAULT now()
)

-- Monthly Usage Tracking
usage_counter (
  id UUID PRIMARY KEY,
  user_id UUID FK (RESTRICT),
  month DATE,                      -- first day of month
  tokens_used INT,
  requests INT,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
)
```

### Redis (Cache & Queue)

```
Namespaces:

1. Rate-Limit State
   Key: rl:rpm:{user_id}:{minute_window}
   Value: COUNT (Lua sliding window)
   TTL: 2 minutes
   
   Key: rl:tpm:{user_id}:{minute_window}
   Value: TOKENS (counter refreshed on window slide)
   TTL: 2 minutes
   
   Key: rl:concurrent:{user_id}
   Value: COUNT
   TTL: PEXPIRE 100ms (refreshed per request)

2. Arq Queue
   Key: arq:job:{job_id}
   Value: JSON task spec
   (Arq manages lifecycle: enqueue → dequeue → ack)

3. Pub/Sub (Job Events)
   Channel: job:{job_id}:events
   Subscribe: SSE endpoint streams events
   Publish: Worker publishes job lifecycle events

4. Cancel Flags
   Key: cancel:{job_id}
   Value: 1 (set on DELETE /jobs/{id})
   TTL: 5 minutes (grace period for SIGTERM)
   Worker checks: cancel flag present? → send SIGTERM

5. Session Cache
   Key: codex:auth:session_hash
   Value: validated | refreshing | failed
   TTL: 1 hour (sync with Codex CLI session TTL)
```

---

## Rate-Limit Model

### Four Dimensions

| Dimension | Window | Logic | Storage | Headers |
|-----------|--------|-------|---------|---------|
| **RPM** | Sliding minute | Lua script (atomic increment + window slide) | Redis key + EXPIRE | X-RateLimit-Limit/Remaining-Requests |
| **TPM** | Sliding minute | Counter (refresh on window boundary) | Redis counter + EXPIRE | X-RateLimit-Limit/Remaining-Tokens |
| **Concurrent** | Real-time | PEXPIRE refresh (100ms TTL) | Redis counter + PEXPIRE | (implicit: 429 if exceeded) |
| **Monthly** | Calendar month | Postgres counter + Redis cache | usage_counter table + redis | (usage endpoint) |

### Lua Script (RPM/Sliding Window)

```lua
-- KEYS[1] = rate_limit_key (e.g., "rl:rpm:user-abc:2026-04-27-12")
-- ARGV[1] = current_timestamp
-- ARGV[2] = window_size_seconds (60)
-- ARGV[3] = max_requests (e.g., 3600 for 60 req/min)

local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_req = tonumber(ARGV[3])

-- Remove old entries outside window
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

-- Count requests in window
local count = redis.call('ZCARD', key)

if count >= max_req then
  return {0, count, max_req}  -- rejected
end

-- Add request
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, window + 1)

return {1, count + 1, max_req}  -- accepted
```

### Headers Example

```
X-RateLimit-Limit-Requests: 3600
X-RateLimit-Remaining-Requests: 3599
X-RateLimit-Reset-Requests: 2026-04-27T13:01:00Z

X-RateLimit-Limit-Tokens: 90000
X-RateLimit-Remaining-Tokens: 89500
X-RateLimit-Reset-Tokens: 2026-04-27T13:01:00Z

Retry-After: 30  (if 429 returned)
```

---

## Authentication Model

### Bearer Token Flow

```
1. User creates API key
   POST /v1/admin/api-keys
   {"name": "dev-key"}
   
2. Server generates + hashes
   raw_key = "sk-" + random(32 bytes)
   key_hash = argon2id(raw_key, salt)
   key_prefix = raw_key[:20]
   
   Store in DB:
     api_keys.key_hash = key_hash
     api_keys.key_prefix = key_prefix
   
   Return: {"key": raw_key} (shown only once)

3. Client stores key
   export OPENAI_API_KEY=sk-abc123...

4. Client request
   POST /v1/chat/completions
   Authorization: Bearer sk-abc123...

5. Gateway auth middleware
   a. Extract token from header
   b. Look up key_prefix in Redis cache
   c. If miss: query Postgres for key_hash
   d. Verify: argon2id.verify(raw_key, key_hash)
   e. Set request.user = User(...)
   
6. Rotate key (optional)
   PUT /v1/admin/api-keys/{id}/rotate
   Server: generate new key_hash, set old status='rotated'
```

### Per-Key Audit Log

```
Every API call → audit_log (async via bg_session):
  user_id, api_key_id, method, path, status, response_size, duration, error

Example query:
  SELECT * FROM audit_log
  WHERE api_key_id = 'key-abc' AND created_at > now() - interval '7 days'
  ORDER BY created_at DESC
```

---

## Workspace & Sandbox Model

### Ephemeral Workspace Lifecycle

```
1. Job creation (gateway or worker)
   mkdir -p /tmp/workspace-{job_id}
   workspace_root = /tmp/workspace-{job_id}
   
2. Clone repo (worker)
   cd {workspace_root}
   git clone --depth=1 -b {branch} {repo_url}
   
3. Run codex (worker)
   codex exec --json \
     --ephemeral \
     --sandbox workspace-write \
     --cd {workspace_root}/repo \
     --skip-git-repo-check \
     "{task}"
   
4. Generate diff
   git diff --no-index /dev/null . > {workspace_root}/diff.patch
   (or use git diff HEAD for in-repo changes)
   
5. Cleanup (janitor or post-response)
   rm -rf /tmp/workspace-{job_id}
   
Note: tmpfs mount (--tmpfs /tmp:size=10G) ensures fast cleanup
```

### Path Safety (C6 Fix)

```python
# Prevent ../../../ escape to /etc/passwd
def validate_path_inside(requested: str, workspace_root: str) -> Path:
    root_real = os.path.realpath(workspace_root)
    path_real = os.path.realpath(requested)
    
    # Ensure commonpath == root (no ../ escape)
    common = os.path.commonpath([root_real, path_real])
    if common != root_real:
        raise InvalidWorkspacePath(...)
    
    return Path(path_real)
```

### Sandbox Enforcement (Codex Built-in)

```
--sandbox workspace-write:
  Linux: Landlock (if kernel ≥ 5.13)
         Fallback: seccomp
  macOS: Seatbelt
  Windows: (not applicable; not in scope)

Prevents:
  - Network access
  - System call access (except allowed subset)
  - File access outside workspace
```

---

## Observability: Logs + Metrics + Traces

### Structured Logging (structlog → Loki)

```
Gateway writes JSON to stdout:
{
  "request_id": "req-1234567890",
  "service": "codex-wrapper",
  "level": "info",
  "event": "chat.completions.started",
  "model": "codex",
  "user_id": "user-abc",
  "ts": "2026-04-27T12:00:00.000Z"
}

Promtail reads stdout → Loki:
  Loki stores with labels:
    job="codex-wrapper"
    service="codex-wrapper"
    instance="vm-prod-1"
  Searchable: {job="codex-wrapper"} | json | level="error"
```

### Metrics (Prometheus)

```
16 instruments (see codebase-summary.md):
  - request_duration_seconds (histogram: p50/p95/p99)
  - request_errors_total (counter by error_type)
  - rate_limit_headroom_tokens (gauge per user)
  - job_queue_depth (gauge: pending jobs)
  - codex_stdout_events_total (counter by event_type)
  - ... (13 more)

Scrape interval: 15s (configurable)
Retention: 15 days (Prometheus server config)
Grafana dashboards: latency, error rate, queue depth, rate-limit, job success
```

### Traces (OpenTelemetry → Tempo)

```
Gateway exports OTLP to Tempo:
Span hierarchy:
  Span: /v1/chat/completions
    ├─ Span: auth.lookup_key
    ├─ Span: rate_limit.check
    ├─ Span: codex.runner.run
    │  ├─ Span: workspace.create
    │  ├─ Span: subprocess.execute
    │  └─ Span: workspace.cleanup
    └─ Span: response.serialize

Tempo stores traces with:
  trace_id (global)
  span_id (per span)
  parent_span_id (hierarchy)
  attributes: user_id, job_id, status, duration_ms
  
Grafana Tempo UI: trace waterfall, latency analysis
```

---

## Cancellation Model

### Job Cancellation Flow

```
1. Client cancels
   DELETE /v1/codex/jobs/{job_id}

2. Gateway route handler
   - Set cancel flag in Redis: cancel:{job_id} = 1
   - Return 202 Accepted
   - TTL: 5 minutes (grace period)

3. Worker checks flag
   Every 1s during codex subprocess:
     if redis.exists(cancel:{job_id}):
       runner.cancel()  # send SIGTERM

4. Runner graceful shutdown
   Send SIGTERM to codex subprocess
   Wait 5 seconds for graceful exit
   If still running: send SIGKILL
   Cleanup workspace immediately
   
5. Job status
   Update: status = "cancelled", error = "Cancelled by user"
   Publish: event type: "job.cancelled"
```

---

## Backup & Disaster Recovery

### Daily Backup (age encrypted)

```bash
# Daily cron (e.g., 02:00 UTC)
#!/bin/bash
set -e

DB_URL=postgresql://user:pass@localhost/codex_wrapper
AGE_RECIPIENT="age1q2w3e..." # Public key from GH secret
S3_BUCKET="s3://backups.internal/codex-wrapper/"

pg_dump "$DB_URL" | \
  age --recipient "$AGE_RECIPIENT" | \
  aws s3 cp - "$S3_BUCKET/$(date +%Y-%m-%d).db.age"

# Keep 30 days retention
aws s3 rm "$S3_BUCKET/" --recursive --exclude "*" \
  --include "*.db.age" \
  --older-than 30
```

### Restore Drill (Quarterly)

```bash
# Verify backup integrity
age --decrypt < backup-2026-04-27.db.age | \
  pg_restore -d test_codex_wrapper

# Validate record counts
psql test_codex_wrapper -c "SELECT COUNT(*) FROM users;"
```

---

## Security Model

### SSRF Defense

```
Validation before any external HTTP call:

1. Parse URL
2. Resolve hostname (check for private IPs)
   - 127.0.0.1, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
   - localhost, *.local
3. Reject if private
4. Use safe transport (requests.Session with custom adapter)
5. Enforce timeout (10s)

Example: git clone validation
  URL: https://github.com/user/repo
  ✓ Public GitHub IP → allowed
  
  URL: http://169.254.169.254/  (AWS metadata)
  ✗ Link-local IP → rejected
```

### Workspace Isolation

```
Per job:
  - Ephemeral /tmp/workspace-{job_id}
  - Process runs in --sandbox workspace-write
  - Path validation (realpath + commonpath)
  - Deleted post-job cleanup

Cross-job contamination:
  - Each job has isolated workspace
  - No shared mutable state
  - Integration test: verify workspace-A can't read workspace-B files
```

### Secret Management

```
Secrets:
  - ChatGPT auth: ~/.codex/auth.json (RO mount, encrypted in backup)
  - Postgres password: env var from .env (not committed)
  - Redis password: env var from .env (not committed)
  - API keys: argon2id hashed in DB (plaintext never stored)
  - GH secrets (AGE_KEY): for backup encryption

Redaction:
  - structlog RedactionProcessor scrubs:
    - "sk-*" patterns
    - "Authorization" header values
    - "auth_json" fields
  - CI grep gate: verify no sk- patterns in logs
```

---

## Deployment Model

### Single VM (Docker Compose)

```
Host:
  /var/lib/codex-wrapper/postgres  (volume)
  /var/lib/codex-wrapper/redis     (volume)
  ~/.codex/                        (RO bind from admin)
  /tmp                             (tmpfs for workspaces)

Containers:
  gateway (port :8000)
  worker (no exposed port)
  postgres (port :5432, internal)
  redis (port :6379, internal)
  caddy (port :80, :443)
  otel-collector (port :4317 for OTLP)
  prometheus (port :9090)
  loki (port :3100)
  tempo (port :4317)
  grafana (port :3000)

Reverse proxy (Caddy):
  Port 80 → redirect HTTPS
  Port 443 → /v1/* → :8000
  
Access gate (external):
  Cloudflare Access / Tailscale / IP allowlist
  Enforces: only internal users reach Caddy
```

### Scaling Considerations

**v1:** Single VM, vertical scale sufficient for < 1k internal users.

**v1.1 / v2:** K8s migration path (not on v1 roadmap):
  - Multiple gateway replicas
  - Worker autoscaling on queue depth
  - Postgres read replicas
  - Redis cluster (for rate-limit + cache)

---

## Observability: Alerting

### Prometheus Alerting Rules

| Alert | Condition | Action |
|-------|-----------|--------|
| HighErrorRate | error_rate > 5% (5m avg) | Page on-call |
| RateLimitExceeded | 429s > 10/min (10m avg) | Investigate user quota |
| QueueBacklog | job_queue_depth > 100 | Scale workers |
| PostgresDown | pg_up = 0 | Critical: manual intervention |
| RedisDown | redis_up = 0 | Critical: manual intervention |
| WorkspaceSize | max workspace > 500MB | Janitor cleanup |
| HighLatency | p95 duration > 10s (10m avg) | Investigate Codex |

---

**Last Updated:** 2026-04-27
