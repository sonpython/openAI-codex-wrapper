# System Architecture: Modules & Data Flow

**Topic:** Module interactions, detailed data flows, tool-calling synthesis

---

## Tool-Calling Synthesis (Home Assistant Extended OpenAI Conversation)

**Status:** Verified working with HA EOC and nested schemas (v1.0 feature, commit 2091772)

### Problem Solved

Home Assistant Extended OpenAI Conversation's `execute_services` tool declares nested array-of-objects schema:
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "domain": { "type": "string" },
      "service": { "type": "string" },
      "service_data": { "type": "object" }
    },
    "required": ["domain", "service", "service_data"]
  }
}
```

Previous prompt-engineering approach (flat `name(param: type)` format) hid nested keys. Codex emitted JSON without `domain` field → HA raised `KeyError: 'domain'` → "Unexpected error during intent recognition".

### Solution: Full JSON Schema Inlining

`src/chat/tool_calling.py::format_tools_prompt()` now inlines each tool's full `parameters` JSON schema (compact form) in the prompt system message.

```python
def format_tools_prompt(
    tools: list[dict[str, Any]],
    tool_choice: object | None = None,
) -> str:
    """
    Inlines full parameters JSON schema for each tool.
    
    Each tool description includes:
      - {name}: {description}
        parameters: {full JSON schema (compact)}
    
    INSTRUCTIONS block teaches Codex:
      - Emit ONLY JSON (no prose wrapper)
      - Arguments MUST conform exactly to schema
      - For nested objects/arrays: include ALL required keys for EVERY item
    """
```

**Result:** Codex now correctly emits:
```json
{
  "tool_calls": [
    {
      "name": "execute_services",
      "arguments": {
        "list": [
          { "domain": "light", "service": "turn_on", "service_data": {"brightness": 200} },
          { "domain": "climate", "service": "set_temperature", "service_data": {"temperature": 22} }
        ]
      }
    }
  ]
}
```

### Response Parsing

`src/chat/tool_calling.py` provides fallback parsing chain:

1. **Markdown fence stripping** — removes ` ```json ... ``` ` wrapper if Codex adds one
2. **Direct JSON parse** — try `json.loads()` first
3. **JSON object extraction** — fallback: find first `{`, match closing `}` by depth tracking
4. **Plain text pass-through** — if no JSON found, return plain response

### Multi-Turn Tool Use Support

Request schemas (`src/gateway/schemas/chat_request.py`) now support:
- `tool_calls` field on `role="assistant"` messages for history replay
- `tool_call_id` + `role="tool"` for tool result messages (multi-turn flow)

Prompt builder includes tool call history in context.

### Test Coverage

27/27 unit tests in `tests/unit/test_tool_calling.py` pass, including:
- `test_format_tools_prompt_inlines_nested_schema` (regression for HA EOC)
- Multi-tool call synthesis
- Fallback parsing with prose

---

## Chat Completions Data Flow (with Tool-Calling)

### Sync Request Flow

```
1. POST /v1/chat/completions
   {
     "model": "codex",
     "messages": [{"role": "user", "content": "..."}],
     "tools": [...],  # Optional
     "tool_choice": "auto"  # Optional
   }

2. Gateway routes request to chat_completions_sync()
   - Middleware: RequestID, Auth, RateLimit
   
3. chat.sync_handler.sync_chat_completions()
   a. Create workspace: /tmp/workspace-{chat_id}
   b. Validate request (image_url rejected, tool_calls schema validated)
   c. Build prompt via prompt_builder.build_prompt_for_codex()
      - Include tool prompt from format_tools_prompt() if tools present
      - Include multi-turn history if previous tool calls in messages
   d. Spawn codex runner: codex exec --json "{full_prompt}"
   
4. Codex Runner (subprocess)
   Reads prompt with tool definitions + instructions
   Emits JSONL events to stdout
   
5. Parse Response
   - Collect all output events
   - If response is JSON object with "tool_calls" key:
     → Parse as tool calls (verify schema compliance)
     → Set finish_reason = "tool_calls"
   - Else:
     → Plain text response
     → Set finish_reason = "stop"
   
6. Return ChatCompletionsResponse
   {
     "choices": [{
       "message": {
         "role": "assistant",
         "content": null,  # null when tool_calls present
         "tool_calls": [
           {"id": "call_abc", "type": "function", "function": {"name": "...", "arguments": "..."}}
         ]
       },
       "finish_reason": "tool_calls"
     }]
   }
```

### Streaming Request Flow

Same as sync, but returns SSE stream:
```
data: {"choices": [{"delta": {"content": "..."}}]}
data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_abc", ...}]}}]}
data: {"choices": [{"finish_reason": "tool_calls"}]}
```

---

## Real-Codex 0.125 Drift Detection

**Weekly cron:** `compat-real-codex.yml` runs against `@openai/codex@latest` with canned fixtures.

Detects:
- JSONL schema breaks
- Tool-calling response format changes
- Event type mismatches

Auto-files GH issue on failure.

---

## Rate-Limit Model (Detailed)

### Four Dimensions

| Dimension | Window | Storage | Logic |
|-----------|--------|---------|-------|
| **RPM** | Sliding 60s | Redis sorted set + Lua script | Atomic: ZREMRANGEBYSCORE + ZCARD + ZADD |
| **TPM** | Sliding 60s | Redis counter + manual slide | Counter; refresh on window boundary |
| **Concurrent** | Real-time | Redis counter + PEXPIRE 100ms | INCR + PEXPIRE; check before increment |
| **Monthly** | Calendar month | Postgres `usage_counter` + Redis cache | Postgres source-of-truth; Redis cache 1h TTL |

### Lua Script (RPM)

Stored in `src/infra/redis_lua/`:

```lua
-- KEYS[1]: rate_limit_key e.g. "rl:rpm:user-abc:2026-04-29-12"
-- ARGV[1]: now (unix timestamp)
-- ARGV[2]: window_size (seconds, e.g. 60)
-- ARGV[3]: max_requests (e.g. 3600 for 60 req/min)

local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_req = tonumber(ARGV[3])

-- Remove old entries outside the window
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

-- Count requests in window
local count = redis.call('ZCARD', key)

if count >= max_req then
  return {0, count, max_req}  -- rejected: {0, current_count, limit}
end

-- Add current request
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, window + 1)

return {1, count + 1, max_req}  -- accepted: {1, new_count, limit}
```

---

## Workspace & Sandbox (C6 Red-Team Fix)

### Path Validation

```python
def validate_path_inside(requested: str, workspace_root: str) -> Path:
    """Prevent ../../../etc/passwd escape."""
    root_real = os.path.realpath(workspace_root)
    path_real = os.path.realpath(requested)
    
    common = os.path.commonpath([root_real, path_real])
    if common != root_real:
        raise InvalidWorkspacePath(...)
    
    return Path(path_real)
```

Prevents: symlink attacks, hard link escape, `..` traversal.

### Sandbox Enforcement

Codex built-in `--sandbox workspace-write`:
- **Linux ≥5.13:** Landlock (first-class sandboxing)
- **Linux <5.13:** seccomp (syscall filtering)
- **macOS:** Seatbelt (XNU sandbox)

Prevents: network access, system call access, file access outside workspace.

---

## Admin UI Module (HTMX + Jinja2 Dashboard)

### Overview

`src/admin_ui/` provides a web-based management console at `/admin/ui/*` (mounted via Caddy at `/admin/ui` in production, `/admin/ui` direct in dev).

### Authentication: Cookie-Session HMAC-SHA256

1. User navigates to `/admin/ui/login`
2. Form posts `ADMIN_TOKEN` value
3. Server computes: `session_hash = HMAC-SHA256(ADMIN_TOKEN, signed_timestamp)`
4. Sets cookie: `__session_hmac` (HttpOnly, SameSite=Strict, TTL 8h)
5. Subsequent requests verify: `HMAC-SHA256(ADMIN_TOKEN, timestamp)` matches cookie
6. Session stored in Redis with TTL for rate-limit / revocation

Key files:
- `src/admin_ui/auth.py` — Sign/verify session, Redis CRUD
- `src/gateway/middleware/auth.py` — Added `/admin/*` to `AUTH_SKIP_PREFIXES` (cookie auth handles `/admin/ui/login`, then redirects)

### Pages & Data Flow

| Page | Route | Data Source | Features |
|------|-------|-------------|----------|
| Dashboard | `/admin/ui/` | Prometheus queries (5s cache) | KPI cards (req rate, error rate, queue depth, active jobs); auto-refresh every 5s via HTMX |
| API Keys | `/admin/ui/keys` | GET `/admin/keys` (auth-required) | Create, revoke, rotate keys; change tier |
| Tiers | `/admin/ui/tiers` | GET/PUT `/admin/tiers` | Edit RPM/TPM/concurrent/monthly; cache invalidation on PUT |
| Users | `/admin/ui/users` | GET `/admin/users` (with LEFT JOIN usage_daily) | Per-user list; detail page with 30-day daily usage Chart.js graph |
| Jobs | `/admin/ui/jobs` | GET `/admin/jobs` (paginated) | Job list with status; modal shows job stderr via proxy |
| Audit | `/admin/ui/audit` | GET `/admin/audit` | Audit log viewer (method, path, status, error, duration) |

### Prometheus Integration

`src/admin_ui/prom_client.py` queries Prometheus at `PROMETHEUS_URL`:

```python
def query_prometheus(expr: str, timeout_seconds: int = 2) -> dict:
    """Query Prometheus via HTTP API with 5s client-side cache."""
    # Caches results in Redis: `prom:cache:{hash(expr)}`
    # Falls back to parsing `/_internal/metrics` text-format if Prometheus unreachable
```

Dashboard polls every 5 seconds; metrics include:
- `rate(codex_wrapper_request_duration_seconds_bucket[1m])` — requests/sec
- `rate(codex_wrapper_request_errors_total[1m])` — errors/sec
- `codex_wrapper_job_queue_depth` — pending jobs
- `count(up{job="gateway"})` — active codex subprocesses

---

## Daily Usage Tracking (`usage_daily` Table)

### Schema

```sql
CREATE TABLE usage_daily (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    api_key_id UUID REFERENCES api_keys(id) ON DELETE SET NULL,
    period DATE NOT NULL,
    requests BIGINT NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, api_key_id, period),
    UNIQUE (api_key_id, period)
);

CREATE INDEX ix_usage_daily_user_period ON usage_daily(user_id, period);
CREATE INDEX ix_usage_daily_api_key_period ON usage_daily(api_key_id, period);
```

### Writers

Two async paths:

1. **Chat Completions middleware** — after route handler completes:
   ```python
   # src/gateway/middleware/usage_tracking.py
   async def track_usage(request, response):
       # Background task queues:
       await usage_daily.upsert(
           user_id, api_key_id, today,
           requests=1,
           input_tokens=estimate(request),
           output_tokens=estimate(response)
       )
   ```

2. **Jobs worker** — after job finishes:
   ```python
   # src/workers/job_handlers.py
   async def run_codex_job(...):
       # After mark_succeeded():
       await usage_daily.upsert(
           user_id, api_key_id, today,
           requests=1,
           input_tokens=job.input_tokens,
           output_tokens=job.output_tokens
       )
   ```

### Atomic Upsert Pattern

Uses SQLAlchemy `pg_insert().on_conflict_do_update()`:

```python
# src/db/crud/usage_daily.py
async def upsert(
    session: AsyncSession,
    user_id: UUID,
    api_key_id: UUID | None,
    period: date,
    requests: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
):
    """Atomically upsert: INSERT OR ADD to existing row."""
    stmt = (
        pg_insert(UsageDaily).values(
            user_id=user_id,
            api_key_id=api_key_id,
            period=period,
            requests=requests,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "api_key_id", "period"],
            set_=dict(
                requests=UsageDaily.requests + requests,
                input_tokens=UsageDaily.input_tokens + input_tokens,
                output_tokens=UsageDaily.output_tokens + output_tokens,
            )
        )
    )
    await session.execute(stmt)
    await session.commit()
```

### Query Pattern (Admin UI)

30-day per-user usage (used by `/admin/ui/users/{user_id}`):

```python
# SELECT all usage_daily rows for a user, past 30 days
# GROUP BY period, SUM requests/tokens
# ORDER BY period DESC
# Result: daily_usage_dict = {date: {requests, input_tokens, output_tokens}}
```

---

**Last Updated:** 2026-05-02 (admin UI, daily usage tracking, Prometheus integration, Phase 07-10 complete)
