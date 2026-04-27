# Phase 06: Multi-Tier Rate Limiting

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§11 tier values; §5 schema `plans` + `usage_counter`)
- OpenAI taxonomy: research/researcher-02-openai-event-taxonomy.md (Part A — `X-RateLimit-*` headers expected by SDKs)
- Phase 01: phase-01-auth-and-models.md (auth middleware exposes `request.state.api_key_id`, `request.state.user_id`, `request.state.tier`)
- Phase 03: phase-03-chat-completions.md (token-counting via tiktoken already wired)
- Phase 05: phase-05-jobs-and-arq.md (POST /v1/codex/jobs hits concurrent cap at enqueue)

## Overview
- Priority: high
- Status: pending
- Effort: M
- Description: Enforce per-API-key, per-tier rate limits across four dimensions: RPM (requests/minute), TPM (tokens/minute), in-flight concurrency, and monthly token quota. Sliding-window counters in Redis via atomic Lua script; monthly counters in Postgres with Redis cache. Emit OpenAI-style `X-RateLimit-*` headers on every response. 429 with proper `Retry-After` on rejection. **Plus**: pre-auth IP rate limiter to prevent argon2-burn DoS amplification.

## Red Team Resolutions
Addresses: **C2** (auth-bypass DoS amplifier — new pre-auth IP limiter component), **C3** (BaseHTTPMiddleware buffers SSE — switch streaming-path middlewares to raw ASGI; headers set in route), **C4** (concurrent counter DECR coverage gaps — Lua TTL refresh on every check + explicit DECR on Redis exceptions + 30s in-stream refresh ping), **C5** (TPM true-up negative-ZSET math wrong — replaced with separate per-window counter via `INCRBYFLOAT`).

This phase is the most heavily revised; multiple architecture changes touch middleware ordering, the Lua scripts, and `app.py` registration.

## Key Insights
- **Sliding window via sorted set + Lua atomic script** (RPM only): one ZSET `rl:rpm:{key_id}`; Lua script removes expired entries (score < now-60s), ZCARD count, conditional ZADD, returns `(count, allowed)`. Atomic = no race between check and increment.
- **TPM via separate per-window INCRBYFLOAT counter** (NOT a ZSET): the negative-cost ZSET approach is unsound — when window eviction removes a long-lived positive entry before its negative offset evicts, the user can dip below zero in the window and steal quota (red team C5). Instead: a Redis key `rl:tpm:{key_id}:{window_id}` holds the running token sum; window_id changes every 60s; counter has TTL = 120s (covers prior window during transition). Upfront: `INCRBYFLOAT key, est`. After response: `INCRBYFLOAT key, (actual - est)` (a NEGATIVE float corrects overestimate). Trade-off: total-only — no per-request audit trail in Redis (audit_log table covers per-request). Math is sound.
  - **Alternative (DEFER decision per Q3)**: skip true-up entirely; charge `max_tokens` estimate upfront and accept slight over-throttling. Simpler reasoning. Lead picks one before phase implementation.
- **Concurrent counter — separate primitive**: `INCR concurrent:{key_id}` on request enter, `DECR` on response close (in `finally`). Cap via tier value. Use Lua atomic INCR+EXPIRE+conditional-DECR (see `concurrent_check.lua` below). **Critical**: `PEXPIRE 60000` MUST be re-set on every Lua invocation (not only on first set) so active keys never lose TTL. **Long streams (>60s)**: runner emits a refresh ping every 30s via `EXPIRE` to keep the counter alive — without this, a 90s stream would let TTL expire mid-flight, drifting the counter and silently breaking concurrent caps for parallel requests. Worker SIGKILL drift is BOUNDED to 60s (next request after TTL sees correct count).
- **Redis exception → explicit DECR**: any `_sw_lua` or `_concurrent_lua` call that may have INCREMENTED the counter MUST be wrapped in try/except: on Redis exception (timeout, connection loss), explicit `await redis.decr(...)` before re-raise. Without this, an INCR-success-then-network-drop scenario drifts the counter UP indefinitely.
- **Monthly quota** (cross-restart durable): Postgres `usage_counter (user_id, period date PK)` aggregated per UTC calendar month. Hot-path uses Redis cache `monthly:{user_id}:{YYYY-MM}` (TTL 60s). On request: check cache → if cached value >= tier limit, reject 429 BEFORE running. Increment after token count known via `INSERT … ON CONFLICT … DO UPDATE` to PG row + Redis INCRBY.
- **OpenAI header parity** (researcher-02 §A — SDK clients parse these): MUST emit `X-RateLimit-Limit-Requests`, `X-RateLimit-Remaining-Requests`, `X-RateLimit-Reset-Requests`, `X-RateLimit-Limit-Tokens`, `X-RateLimit-Remaining-Tokens`, `X-RateLimit-Reset-Tokens`. Reset values are seconds-until-reset (e.g., `1m20s` or absolute? OpenAI uses formats like `7m12s` for tokens, `60s` for requests — emit string form for fidelity). 429 response also adds `Retry-After: <seconds>` integer.
- **Tier seeded via migration** (brainstorm §11):
  | tier | RPM | TPM | concurrent | monthly_tokens |
  |---|---|---|---|---|
  | free | 20 | 20000 | 2 | 100000 |
  | pro | 200 | 200000 | 10 | 2000000 |
  | enterprise | 2000 | 2000000 | 50 | 20000000 |
- **Pre-auth IP rate limiter (NEW — C2 fix)**: an unauthenticated request with malformed/missing `Authorization` header is fully passed through `AuthMiddleware` (which 401s) — but argon2 verify costs ~30-100ms per attempt. An attacker spamming garbage tokens at ~100 RPS burns one CPU forever. Solution: a `EdgeIPLimiter` middleware runs FIRST (before `AuthMiddleware`); when the request has no `Authorization` header OR the header doesn't parse as `Bearer cwk_<24-suffix>`, increment a per-IP bucket `ip_pre_auth:{ip}` (Redis INCR + TTL 60s), reject 429 if over `IP_PRE_AUTH_RPM` (default 30/min/IP) BEFORE running any argon2 work. Authenticated requests with VALID-shaped tokens skip this gate (still subject to AuthMiddleware verify). Phase-08 alert rule fires on `auth_rejections_total{reason='ip_pre_auth_429'} > 100/min`.
- **Middleware ordering — EXACT** (FastAPI executes middlewares in REVERSE registration order on the request, forward order on the response):
  - REQUEST flow: `EdgeIPLimiter (NEW)` → `AuthMiddleware` → `RateLimitMiddleware` → `UsageTrackingMiddleware` → route
  - RESPONSE flow: route → `UsageTrackingMiddleware` → `RateLimitMiddleware` → `AuthMiddleware` → `EdgeIPLimiter`
  - Registration in `app.py` (last-added-runs-first on request): `add_middleware(UsageTracking)`, `add_middleware(RateLimit)`, `add_middleware(Auth)`, `add_middleware(EdgeIPLimiter)` — last add is outermost wrap = first to see request.
- **Raw ASGI middleware on streaming path (C3 fix)**: `BaseHTTPMiddleware` buffers `StreamingResponse` / `EventSourceResponse` bodies into memory, breaking SSE first-byte latency (Starlette #1012, FastAPI #5536). `RateLimitMiddleware` and `UsageTrackingMiddleware` MUST be implemented as raw ASGI middleware (`async def __call__(self, scope, receive, send)`) for any path that touches streaming. Non-streaming routes still work, but the same code path is used uniformly. Pattern: middleware computes limits + headers dict, stashes via `scope["state"]["rate_limit_headers"]`; **route layer** reads the dict and merges into `EventSourceResponse(headers=...)` / `StreamingResponse(headers=...)` constructor. Header injection NEVER happens via `response.headers["X-RateLimit-..."] = ...` post-call on streaming responses.
- **Sync (non-stream) routes**: can rely on the middleware-injected headers via `send` wrapper. The header dict is the same; the difference is the route layer doesn't have to opt in. Both sync and stream routes read from `scope["state"]["rate_limit_headers"]` for safety.

## Requirements

### Functional
- Reject request with HTTP 429 if any of {RPM, TPM, concurrent, monthly} would be exceeded by this request.
- Emit `X-RateLimit-*` headers on every successful response (2xx and even 429).
- 429 response body matches OpenAI shape: `{"error":{"type":"rate_limit_exceeded","code":"rate_limit_exceeded","message":"...","param":null}}` plus `Retry-After: <int seconds>`.
- TPM estimation: chat/responses → tiktoken on prompt + `max_tokens` (or 1024 default); jobs → fixed 0 cost (jobs use Codex CLI runtime, not tokenized).
- Monthly counter increments only on 2xx responses; 4xx/5xx don't burn quota.
- Concurrent counter: increments on auth success (request enter), decrements on response complete OR client disconnect.
- Tier table mutable via migration; seed migration for `free`/`pro`/`enterprise`.

### Non-Functional
- Lua script ≤ 100 lines; runs in single Redis round-trip.
- Middleware overhead p99 < 5ms per request.
- Files: `middleware/rate_limit.py` ≤ 200 LOC, `middleware/usage_tracking.py` ≤ 200 LOC, `infra/redis_lua/sliding_window.lua` ≤ 60 lines.
- Metrics: emit `rate_limit_check_duration_seconds`, `rate_limit_rejections_total{dimension}`.
- Tier-config caching: tier→limits dict cached in-process (TTL 5min) since plans table changes are rare.

## Architecture

```
client request
   │
   ▼
EdgeIPLimiter (NEW raw-ASGI middleware — outermost wrap)
  ├─ extract ip from x-forwarded-for or scope.client[0]
  ├─ if Authorization header missing OR not "Bearer cwk_..." shape:
  │     INCR ip_pre_auth:{ip} (TTL 60)
  │     if count > IP_PRE_AUTH_RPM (default 30): reject 429 generic OpenAI shape
  │  → bypass argon2 burn entirely
  └─ else: pass through (auth middleware handles real verify)
   │
   ▼
AuthMiddleware (phase 01)
  → request.state.api_key_id, user_id, tier
   │
   ▼
RateLimitMiddleware (raw ASGI — NOT BaseHTTPMiddleware; SSE-safe)
  ├─ load tier limits (in-mem cache, fallback DB)
  ├─ check monthly quota (Redis cache → fallback DB)         ── reject 429 if 0 remain
  ├─ Lua concurrent_check (atomic INCR + PEXPIRE 60000 + conditional DECR) ── reject 429 if over
  ├─ try:
  │     Lua sliding_window for RPM                            ── reject 429 if over (DECR concurrent on reject)
  │     Lua tpm_check for TPM (per-window counter, see below) ── reject 429 if over (DECR concurrent on reject)
  │   except RedisError as e:
  │     await redis.decr(concurrent:{key_id})                 ── compensate the INCR
  │     raise
  ├─ stash headers dict in scope["state"]["rate_limit_headers"]
  ├─ wrap send() to inject X-RateLimit-* on http.response.start (works for sync AND stream)
  ├─ launch background _refresh_concurrent_ttl every 30s while request scope alive (long-stream cover)
  └─ finally (response sent OR exception): await redis.decr(concurrent:{key_id})
   │
   ▼
UsageTrackingMiddleware (raw ASGI — NOT BaseHTTPMiddleware; passes streaming bodies through)
  ├─ pass scope/receive/send through; observe http.response.start status
  ├─ on http.response.body more=False (final chunk): schedule background:
  │     - actual_tokens = scope["state"]["usage"].total_tokens (set by chat/responses route)
  │     - delta = actual - estimated
  │     - INCRBYFLOAT rl:tpm:{key_id}:{window_id} delta            (sound math — see C5 fix)
  │     - INSERT/UPDATE usage_counter
  │     - Redis INCRBY monthly:{user_id}:{period}
  │   (failures swallowed; logged WARN; do not affect response)
   │
   ▼
route handler
  ├─ stream path: reads scope["state"]["rate_limit_headers"]; passes to EventSourceResponse(headers=...)
  └─ sync path: middleware send-wrap already injected; no extra work
```

### Sliding-window Lua script — RPM only (`sliding_window.lua`)

ZSET-based; one entry per request; ZCARD counts.

```
KEYS[1] = "rl:rpm:{key_id}"
ARGV[1] = now_ms
ARGV[2] = window_ms (60000)
ARGV[3] = limit (tier RPM)
ARGV[4] = entry_id (UUID — avoids collision in tied scores)

1. ZREMRANGEBYSCORE key 0 (now_ms - window_ms)        -- evict expired
2. count = ZCARD key
3. if count + 1 > limit:
       remaining = max(0, limit - count)
       oldest = ZRANGE key 0 0 WITHSCORES  ; reset_ms = (oldest_score + window_ms) - now_ms
       return {0, count, remaining, reset_ms, limit}     -- denied
4. ZADD key now_ms entry_id
5. PEXPIRE key window_ms
6. return {1, count + 1, limit - (count+1), window_ms, limit}  -- allowed
```

### TPM Lua script — per-window counter (`tpm_check.lua`)

**Replaces the broken negative-cost ZSET approach (red team C5).** Uses a single bucket key per 60s window so `INCRBYFLOAT` semantics are total-only and arithmetic is sound.

```
KEYS[1] = "rl:tpm:{key_id}:{window_id}"   ; window_id = floor(now_ms / window_ms)
ARGV[1] = now_ms
ARGV[2] = window_ms (60000)
ARGV[3] = limit (tier TPM)
ARGV[4] = cost (estimate; positive float)

1. current = tonumber(redis.call('GET', KEYS[1]) or '0')
2. if current + cost > limit:
       reset_ms = ((window_id + 1) * window_ms) - now_ms
       return {0, current, math.max(0, limit - current), reset_ms, limit}      -- denied
3. new = redis.call('INCRBYFLOAT', KEYS[1], cost)
4. redis.call('PEXPIRE', KEYS[1], window_ms * 2)        -- 2x TTL covers prior-window key during transition
5. return {1, tonumber(new), limit - tonumber(new), window_ms, limit}           -- allowed
```

**True-up** (post-response): the same key receives `INCRBYFLOAT KEY (actual - est)` directly from `UsageTrackingMiddleware`. If `actual < est` the delta is negative — counter decreases, freeing quota. The key is total-only — no per-request audit trail in Redis (audit_log table provides per-request audit). Window boundary is hard (counter rotates every 60s); accept slight unfairness across boundaries (a request just before t=60s and just after sees counter reset — acceptable trade-off for correctness).

**Decision (Q3)**: implement true-up via the per-window counter as described above. The simpler "no true-up, charge max_tokens" alternative is documented but NOT chosen.

### Concurrent counter Lua (`concurrent_check.lua`)

Atomic INCR + TTL refresh + conditional DECR; PEXPIRE ALWAYS (not first-set-only) so active long-running keys never expire mid-request.

```
local v = redis.call('INCR', KEYS[1])
redis.call('PEXPIRE', KEYS[1], 60000)        -- ALWAYS refresh, not just on set
if v > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return v
```

Returns `0` on reject, `>=1` on accept.

#### Long-stream concurrent counter refresh (C4 fix)

Streams routinely exceed 60s. Without periodic refresh, the counter's TTL expires WHILE the request still holds it → next concurrent INCR sees 1 instead of 2 → cap silently bypassed. Mitigation:

- `RateLimitMiddleware` schedules `_refresh_concurrent_ttl(key_id)` as an asyncio task: `while True: await asyncio.sleep(30); await redis.expire(f"concurrent:{key_id}", 60)`. Task is cancelled in the middleware's `finally` block when request completes.
- Worker SIGKILL (process killed mid-request) → cancellation NOT executed → 60s TTL recovers naturally. Drift is BOUNDED to 60s. Documented as accepted behavior.

### Concurrent counter compensation on Redis exceptions (C4 fix)

```
try:
    rpm_result = await self._sw_lua(...)             # may have ZADD'd
    if rpm_result[0] == 0: ...                       # 429 — concurrent finally-DECR covers
    tpm_result = await self._tpm_lua(...)            # may have INCRBYFLOAT'd
    if tpm_result[0] == 0: ...                       # 429 — concurrent finally-DECR covers
except RedisError:
    await redis.decr(f"concurrent:{key_id}")         # compensate the prior concurrent INCR
    raise
```

Without this compensator, a network drop between `concurrent_check` SUCCESS and the next Redis call leaves the counter incremented forever (the `finally:` DECR also runs, so net is still wrong by zero — UNLESS the exception happened mid-Lua-execution, where Redis still applied the INCR but client never saw the response). Defense-in-depth.

## Related Code Files

### To create
- `src/gateway/middleware/edge_ip_limit.py` (≤ 120 LOC, raw ASGI) — pre-auth per-IP bucket; rejects garbage tokens before argon2.
- `src/gateway/middleware/rate_limit.py` (≤ 200 LOC, raw ASGI) — middleware orchestrator + Lua loader + tier-cache + concurrent-TTL refresher
- `src/gateway/middleware/usage_tracking.py` (≤ 200 LOC, raw ASGI) — header injection on send-wrap + post-response true-up + monthly increment
- `src/infra/redis_lua/sliding_window.lua` (≤ 50 lines, RPM only)
- `src/infra/redis_lua/tpm_check.lua` (≤ 30 lines, per-window INCRBYFLOAT)
- `src/infra/redis_lua/concurrent_check.lua` (≤ 20 lines)
- `src/infra/redis_lua/edge_ip_check.lua` (≤ 20 lines) — INCR + EXPIRE + over-limit check
- `src/infra/redis_lua/__init__.py` — loader: read .lua file, register via `redis.register_script`
- `src/db/crud/plans.py` — get tier limits, in-mem TTL cache
- `src/db/crud/usage_counter.py` — month-rollup increment with INSERT … ON CONFLICT
- `src/db/migrations/versions/00X_plans_seed.py` — migration creating `plans` table + seeding 3 tiers + creating `usage_counter` table
- `src/gateway/rate_limit_errors.py` (≤ 80 LOC) — OpenAI-shaped error builder + Retry-After helper
- `tests/unit/test_sliding_window_lua.py` — lua script via fakeredis or live redis
- `tests/unit/test_tpm_lua.py` — per-window counter math (estimate, true-up, window boundary)
- `tests/unit/test_concurrent_lua.py` — TTL refresh on every call, atomic over-cap rejection
- `tests/unit/test_edge_ip_limit.py` — malformed Authorization → 429 before argon2 verify called
- `tests/unit/test_rate_limit_headers.py`
- `tests/integration/test_rate_limit_429.py` — burst 21 RPM at free tier, 21st returns 429
- `tests/integration/test_long_stream_concurrent.py` — 90s stream + parallel request; cap enforced correctly through TTL refresh
- `tests/integration/test_pre_auth_dos.py` — flood 100 garbage tokens; argon2 verify NOT called (mock + counter)

### To modify
- `src/gateway/app.py` — register rate_limit + usage_tracking middlewares in correct order
- `src/db/models.py` — add `Plan` and `UsageCounter` models
- `src/gateway/routes/chat.py` (phase 03) — set `request.state.usage` after stream/sync completion (true-up source)
- `src/gateway/routes/responses.py` (phase 04) — same
- `src/gateway/routes/jobs.py` (phase 05) — pre-enqueue concurrent check (already enforced by middleware; confirm)
- `src/settings.py` — add `RATE_LIMIT_BYPASS=false` for dev/test, `IP_PRE_AUTH_RPM=30`, `TRUST_PROXY=false` (set true behind Caddy in prod)

### To delete
- (none)

## Implementation Steps

1. **Migration `00X_plans_seed.py`**:
   ```sql
   CREATE TABLE plans (
     tier TEXT PRIMARY KEY,
     rpm INTEGER NOT NULL,
     tpm INTEGER NOT NULL,
     concurrent INTEGER NOT NULL,
     monthly_tokens BIGINT NOT NULL
   );
   INSERT INTO plans VALUES
     ('free', 20, 20000, 2, 100000),
     ('pro', 200, 200000, 10, 2000000),
     ('enterprise', 2000, 2000000, 50, 20000000);

   CREATE TABLE usage_counter (
     user_id UUID NOT NULL REFERENCES users(id),
     period DATE NOT NULL,                         -- first of month UTC
     requests BIGINT NOT NULL DEFAULT 0,
     input_tokens BIGINT NOT NULL DEFAULT 0,
     output_tokens BIGINT NOT NULL DEFAULT 0,
     PRIMARY KEY (user_id, period)
   );
   ```

2. **Lua loader** (`infra/redis_lua/__init__.py`):
   ```python
   def load_script(redis, name):
       path = Path(__file__).parent / f"{name}.lua"
       return redis.register_script(path.read_text())
   ```
   Cache compiled scripts at module init (single SHA per script, EVALSHA fast path).

3. **Lua scripts** (in `src/infra/redis_lua/`):
   - `sliding_window.lua` (RPM): per the pseudocode in Architecture. Returns 5-tuple `[allowed, current, remaining, reset_ms, limit]`.
   - `tpm_check.lua` (TPM): per the pseudocode in Architecture. Single key per 60s window via `floor(now_ms / 60000)`; INCRBYFLOAT for sound math.
   - `concurrent_check.lua`: INCR + ALWAYS-PEXPIRE + conditional DECR per Architecture. Returns `0` on reject, `>=1` on accept.
   - `edge_ip_check.lua`: INCR + EXPIRE 60 + check over `IP_PRE_AUTH_RPM`; returns `0` on reject, `>=1` on accept.

4. **(merged into step 3)**

5. **`db/crud/plans.py`**:
   ```python
   _CACHE = {}        # tier → (limits, expires_at)
   async def get_limits(db, tier: str) -> dict:
       if tier in _CACHE and _CACHE[tier][1] > time.time():
           return _CACHE[tier][0]
       row = await db.execute(select(Plan).where(Plan.tier==tier)).scalar_one()
       limits = {"rpm": row.rpm, "tpm": row.tpm, "concurrent": row.concurrent, "monthly_tokens": row.monthly_tokens}
       _CACHE[tier] = (limits, time.time() + 300)
       return limits
   ```

6. **`db/crud/usage_counter.py`**:
   ```python
   async def increment(db, user_id, period_date, requests, input_tokens, output_tokens):
       stmt = pg_insert(UsageCounter).values(
           user_id=user_id, period=period_date,
           requests=requests, input_tokens=input_tokens, output_tokens=output_tokens
       ).on_conflict_do_update(
           index_elements=["user_id","period"],
           set_={
               "requests": UsageCounter.requests + requests,
               "input_tokens": UsageCounter.input_tokens + input_tokens,
               "output_tokens": UsageCounter.output_tokens + output_tokens,
           }
       )
       await db.execute(stmt); await db.commit()
   ```

7. **Rate-limit middleware** (`middleware/rate_limit.py`) — **raw ASGI middleware (NOT BaseHTTPMiddleware)** so SSE bodies pass through unbuffered:
   ```python
   class RateLimitMiddleware:
       def __init__(self, app, redis, db):
           self.app = app
           self.redis = redis
           self.db = db

       async def __call__(self, scope, receive, send):
           if scope["type"] != "http":
               return await self.app(scope, receive, send)
           # Auth populated state earlier; if missing, AuthMiddleware already rejected.
           api_key_id = scope.get("state", {}).get("api_key_id")
           if not api_key_id or settings.RATE_LIMIT_BYPASS:
               return await self.app(scope, receive, send)

           tier = scope["state"]["tier"]
           user_id = scope["state"]["user_id"]
           limits = await plans.get_limits(self.db, tier)

           # 1. monthly check (Redis-cached)
           period = month_start_utc()
           cached = await self.redis.get(f"monthly:{user_id}:{period}")
           if cached and int(cached) >= limits["monthly_tokens"]:
               return await self._reject_send(send, "monthly_quota_exceeded", retry=seconds_until_next_month())

           # 2. concurrent — Lua atomic
           ok = await self._concurrent_lua(keys=[f"concurrent:{api_key_id}"], args=[limits["concurrent"]])
           if not ok:
               return await self._reject_send(send, "concurrent_limit_exceeded", retry=1)

           # Schedule TTL refresh task for long streams (>60s)
           refresh_task = asyncio.create_task(self._refresh_concurrent_ttl(api_key_id))

           try:
               window_id = int(time.time()) // 60
               try:
                   # 3. RPM sliding window
                   r = await self._sw_lua(
                       keys=[f"rl:rpm:{api_key_id}"],
                       args=[now_ms(), 60000, limits["rpm"], uuid()],
                   )
                   if r[0] == 0:
                       return await self._reject_send(send, "rpm_exceeded", retry=ceil(r[3]/1000))

                   # 4. TPM per-window counter
                   est = estimate_tokens(scope)               # peek body once, cache on scope
                   r2 = await self._tpm_lua(
                       keys=[f"rl:tpm:{api_key_id}:{window_id}"],
                       args=[now_ms(), 60000, limits["tpm"], est],
                   )
                   if r2[0] == 0:
                       return await self._reject_send(send, "tpm_exceeded", retry=ceil(r2[3]/1000))
               except RedisError:
                   # Compensate the concurrent INCR; finally also DECRs (net = -1) — accept double-DECR
                   # only if we treat key as "drift down OK" (prefer over drift-up). Actually we MUST
                   # NOT DECR twice — instead set a guard:
                   raise   # finally handles the single DECR

               # Headers snapshot for downstream (route + sync send-wrap)
               scope.setdefault("state", {})["rate_limit_headers"] = {
                   "X-RateLimit-Limit-Requests": str(limits["rpm"]),
                   "X-RateLimit-Remaining-Requests": str(r[2]),
                   "X-RateLimit-Reset-Requests": format_reset_ms(r[3]),
                   "X-RateLimit-Limit-Tokens": str(limits["tpm"]),
                   "X-RateLimit-Remaining-Tokens": str(int(r2[2])),
                   "X-RateLimit-Reset-Tokens": format_reset_ms(r2[3]),
               }
               scope["state"]["tpm_estimated_cost"] = est
               scope["state"]["tpm_window_id"] = window_id

               # send-wrap: inject headers on http.response.start (works for sync AND stream)
               async def send_wrap(message):
                   if message["type"] == "http.response.start":
                       headers = list(message.get("headers", []))
                       for k, v in scope["state"]["rate_limit_headers"].items():
                           headers.append((k.lower().encode(), v.encode()))
                       message["headers"] = headers
                   await send(message)

               await self.app(scope, receive, send_wrap)
           finally:
               refresh_task.cancel()
               try:
                   await self.redis.decr(f"concurrent:{api_key_id}")
               except RedisError:
                   logger.warning("concurrent_decr_failed", key_id=api_key_id)

       async def _refresh_concurrent_ttl(self, key_id):
           # Long-stream cover: refresh TTL every 30s. Cancelled in finally when request ends.
           try:
               while True:
                   await asyncio.sleep(30)
                   await self.redis.expire(f"concurrent:{key_id}", 60)
           except asyncio.CancelledError:
               return
   ```
   Notes:
   - `_reject_send(send, code, retry)` writes a 429 ASGI response (http.response.start + http.response.body). Defined in `rate_limit_errors.py`.
   - `concurrent` DECR happens in `finally` — covers every return path (success, 429, exception). The Redis-exception compensation in the C4 fix is implicit: the single `finally` DECR covers the prior INCR (Lua atomic ensured INCR happened iff `ok=True`).
   - `estimate_tokens(scope)` peeks JSON body via ASGI receive replay pattern; caches on scope to avoid double-read. See step 11.

8. **Usage tracking middleware** (`middleware/usage_tracking.py`) — **raw ASGI middleware**; observes response status + final body chunk to schedule true-up:
   ```python
   class UsageTrackingMiddleware:
       def __init__(self, app, redis, db):
           self.app = app
           self.redis = redis
           self.db = db

       async def __call__(self, scope, receive, send):
           if scope["type"] != "http":
               return await self.app(scope, receive, send)
           status_code = {"value": 0}

           async def send_wrap(message):
               if message["type"] == "http.response.start":
                   status_code["value"] = message["status"]
               if message["type"] == "http.response.body" and not message.get("more_body", False):
                   # Final chunk seen; schedule true-up after response fully flushed.
                   if status_code["value"] < 400:
                       asyncio.create_task(self._true_up(scope))   # use BG_TASKS pattern (phase-01)
               await send(message)

           await self.app(scope, receive, send_wrap)

       async def _true_up(self, scope):
           try:
               state = scope.get("state", {})
               usage = state.get("usage")
               if not usage:
                   return
               api_key_id = state["api_key_id"]
               user_id = state["user_id"]
               est = state.get("tpm_estimated_cost", 0)
               window_id = state.get("tpm_window_id")
               actual = usage["total_tokens"]
               delta = float(actual - est)
               if delta != 0 and window_id is not None:
                   await self.redis.eval(
                       "redis.call('INCRBYFLOAT', KEYS[1], ARGV[1]); redis.call('PEXPIRE', KEYS[1], 120000); return 1",
                       1,
                       f"rl:tpm:{api_key_id}:{window_id}",
                       str(delta),
                   )
               # monthly
               period = month_start_utc()
               await self.redis.incrby(f"monthly:{user_id}:{period}", actual)
               await self.redis.expire(f"monthly:{user_id}:{period}", 86400)
               await usage_counter.increment(self.db, user_id, period, 1, usage["input_tokens"], usage["output_tokens"])
           except Exception:
               logger.warning("usage_trueup_failed", exc_info=True)
   ```
   Notes:
   - `INCRBYFLOAT` on the SAME per-window key as upfront — corrects sound (positive or negative). No ZSET, no eviction-driven math errors.
   - True-up failures are swallowed; metric `usage_trueup_errors_total` bumped (phase 07).
   - Use `_BG_TASKS` set tracking pattern from phase-01 to prevent task GC.

9. **`format_reset_ms(ms)`** — return human form `"7m12s"` if ≥ 60s else `"800ms"`. Matches OpenAI examples in researcher-02 §A.

10. **Wire middleware order in `app.py`** (FastAPI: last-added is outermost wrap, runs FIRST on request):
    ```python
    app.add_middleware(UsageTrackingMiddleware)   # innermost (closest to route): observes status + body
    app.add_middleware(RateLimitMiddleware)       # outer of UsageTracking; counter INCR/DECR + headers
    app.add_middleware(AuthMiddleware)            # outer of RateLimit; populates state.api_key_id
    app.add_middleware(EdgeIPLimiter)             # outermost: pre-auth IP gate; runs FIRST on request
    ```
    REQUEST flow: `EdgeIPLimiter → AuthMiddleware → RateLimitMiddleware → UsageTrackingMiddleware → route`. RESPONSE flow reverses. Verify with integration tests:
    - Request without `Authorization` header → EdgeIPLimiter 429 after `IP_PRE_AUTH_RPM` exceeded; AuthMiddleware never runs (assert via mock argon2 verify call counter).
    - Authenticated request → AuthMiddleware populates state; RateLimit reads it; UsageTracking observes body completion.
    - All middlewares are raw ASGI (`async def __call__(self, scope, receive, send)`); none use `BaseHTTPMiddleware` (Starlette #1012).

11. **Edge IP Limiter** (`middleware/edge_ip_limit.py`) — raw ASGI; runs before AuthMiddleware:
    ```python
    class EdgeIPLimiter:
        def __init__(self, app, redis):
            self.app = app
            self.redis = redis

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            ip = self._client_ip(scope)
            auth = self._get_header(scope, b"authorization")
            # Token shape: "Bearer cwk_<24+chars>"
            if not auth or not re.match(rb"^[Bb]earer\s+cwk_[A-Za-z0-9]{24,}$", auth):
                ok = await self._edge_ip_check(keys=[f"ip_pre_auth:{ip}"], args=[settings.IP_PRE_AUTH_RPM])
                if not ok:
                    metrics.auth_rejections_total.labels(reason="ip_pre_auth_429").inc()
                    return await reject_429_send(send, "ip_pre_auth_exceeded", retry=60)
            await self.app(scope, receive, send)

        def _client_ip(self, scope):
            # Trust X-Forwarded-For if behind Caddy (set TRUST_PROXY=true in prod)
            for k, v in scope.get("headers", []):
                if k == b"x-forwarded-for" and settings.TRUST_PROXY:
                    return v.decode().split(",")[0].strip()
            client = scope.get("client")
            return client[0] if client else "unknown"
    ```
    Phase-08 alert rule: fire on `auth_rejections_total{reason='ip_pre_auth_429'} > 100/min` — likely scan in progress.

12. **Token estimation** (`estimate_tokens(scope)`) — peeks JSON body via ASGI receive replay; caches result + raw body bytes on `scope["state"]` to avoid double-read:
    - Endpoint = `/v1/chat/completions` or `/v1/responses`: drain `receive()` once, cache bytes; parse JSON; compute `tiktoken.encoding_for_model(model).encode(prompt)` length + `max_tokens` (default 1024). Catch tiktoken exceptions → fallback `len(prompt) // 4`.
    - `/v1/codex/jobs*`: return 0 (jobs are runtime-bound, not token-bound).
    - `/v1/models`: return 0.
    - Subsequent middlewares / routes use the cached body via a `replay_receive` shim (standard FastAPI pattern for body peeking).

13. **`rate_limit_errors.py`** — `_reject(code, retry)`:
    ```python
    return JSONResponse(
        status_code=429,
        content={"error": {"type": "rate_limit_exceeded", "code": code, "message": MESSAGES[code], "param": None}},
        headers={"Retry-After": str(retry), ...rate_limit_headers},
    )
    ```

14. **Tests**:
    - `test_sliding_window_lua.py`: with fakeredis (or live test redis), call script in tight loop; assert allowed/denied transitions, reset_ms decreases monotonically.
    - `test_tpm_lua.py`: per-window counter — upfront INCRBYFLOAT(1000), true-up INCRBYFLOAT(-500), GET == 500. Window boundary: 60s elapsed → new window_id → counter resets to 0 (correct).
    - `test_concurrent_lua.py`: 3 INCRs at cap=2 → third returns 0 (rejected); subsequent calls still PEXPIRE the key (GET TTL > 0 after each call).
    - `test_edge_ip_limit.py`: malformed `Authorization: garbage` → 31st request in 60s gets 429; assert mock argon2.verify called 0 times.
    - `test_rate_limit_headers.py`: hit endpoint, assert all 6 `X-RateLimit-*` headers + `Retry-After` only on 429.
    - `test_rate_limit_429.py`: free tier (20 RPM); fire 21 sequential requests; 21st returns 429 with `Retry-After`. Use freezegun or mock `now_ms` to remove clock-jitter flake.
    - `test_concurrent_cap.py`: free tier (concurrent=2); fire 3 long-running streams in parallel; 3rd gets 429.
    - `test_long_stream_concurrent.py`: stream lasting 90s + 1 parallel chat request — assert (a) stream not killed by TTL expiry, (b) parallel request sees concurrent=2 (correctly accounted), (c) `EXPIRE` called at least 2x during the 90s window (TTL refresh task active).
    - `test_monthly_quota.py`: pre-seed `usage_counter` near limit; next request 429; verify Redis cache populated.
    - `test_tpm_trueup.py`: estimated 1k, actual 500 → assert per-window counter reflects net 500 after true-up; same window_id; no negative entries anywhere.
    - `test_pre_auth_dos.py`: flood 100 garbage Authorization tokens; assert argon2.verify called 0 times (mock); 31st returns 429 from EdgeIPLimiter.
    - **Real-uvicorn + Caddy integration test (C3 sentinel)**: spawn full stack; stream a chat completion; assert first SSE byte arrives at the wire within 1s of subprocess emission. Without this gate, BaseHTTPMiddleware regressions slip through.

## Todo List
- [ ] Migration: `plans` table + seed + `usage_counter` table
- [ ] `Plan` and `UsageCounter` SQLAlchemy models
- [ ] `db/crud/plans.py` with TTL cache
- [ ] `db/crud/usage_counter.py` with upsert
- [ ] `infra/redis_lua/sliding_window.lua` (RPM, ZSET-based)
- [ ] `infra/redis_lua/tpm_check.lua` (per-window counter, INCRBYFLOAT)
- [ ] `infra/redis_lua/concurrent_check.lua` (ALWAYS PEXPIRE on every call)
- [ ] `infra/redis_lua/edge_ip_check.lua`
- [ ] `infra/redis_lua/__init__.py` script loader
- [ ] `middleware/edge_ip_limit.py` raw ASGI pre-auth IP limiter (C2)
- [ ] `middleware/rate_limit.py` raw ASGI orchestrator (C3); concurrent-TTL refresh task (C4); explicit DECR on Redis errors
- [ ] `middleware/usage_tracking.py` raw ASGI; send-wrap inspects status; INCRBYFLOAT true-up (C5)
- [ ] `rate_limit_errors.py` 429 builder + format_reset_ms (raw-ASGI compatible)
- [ ] Token estimator with body-replay receive shim (no double-read)
- [ ] Wire middleware order: `EdgeIPLimiter > Auth > RateLimit > UsageTracking` in `app.py`
- [ ] Routes set `scope["state"]["usage"]` post-response (chat, responses); also pass `scope["state"]["rate_limit_headers"]` to `EventSourceResponse(headers=...)` on streaming routes
- [ ] `RATE_LIMIT_BYPASS`, `IP_PRE_AUTH_RPM=30`, `TRUST_PROXY` settings
- [ ] All unit + integration tests pass (incl. long-stream, pre-auth DoS, real-uvicorn first-byte sentinel)
- [ ] Metrics: `rate_limit_rejections_total{dimension}` counter; `auth_rejections_total{reason}` for ip_pre_auth_429 alert

## Success Criteria
- 21st request in 60s on free tier returns 429 with `Retry-After` ∈ [1, 60].
- 3rd concurrent SSE stream on free tier returns 429 immediately.
- Pre-seeded user at 99% monthly quota: response containing 2k tokens triggers 429 on next request (cache populated within 60s).
- All 2xx responses carry 6 `X-RateLimit-*` headers; values decrease monotonically across burst.
- **Streaming response: headers present in `EventSourceResponse(headers=...)` from route layer; verified BEFORE first SSE chunk byte through real uvicorn + Caddy.**
- **First SSE byte arrives at the wire within 1s of subprocess emission (real-uvicorn + Caddy sentinel test) — proves RateLimit + UsageTracking middlewares do NOT buffer the body.**
- **Pre-auth IP limit: 100 garbage tokens at 100 RPS triggers 429 from EdgeIPLimiter at ~31st attempt; argon2.verify call counter remains 0.**
- Crash mid-request (kill -9): concurrent counter resets within 60s (TTL).
- **90-second stream + parallel chat: cap correctly enforced (test asserts EXPIRE called >=2x during the 90s).**
- Tier limits configurable via DB without code change.
- `pytest tests/integration/test_rate_limit_429.py` passes within 5s (no flake; uses freezegun).
- p99 middleware overhead < 5ms measured via histogram (raw-ASGI overhead is lower than BaseHTTPMiddleware).

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Lua script bug → all requests rejected | Low | Critical | Unit tests per branch; `RATE_LIMIT_BYPASS=true` escape hatch in dev/staging; gradual rollout |
| TPM per-window key not expiring → memory growth | Low | Med | `PEXPIRE` set to `2 * window_ms` on every INCRBYFLOAT; window keys rotate every 60s — only 2 keys per user live at any time |
| BaseHTTPMiddleware buffers SSE → first-byte > 2s in prod | High | Critical | RateLimitMiddleware + UsageTrackingMiddleware are RAW ASGI (not BaseHTTPMiddleware); real-uvicorn+Caddy sentinel test gates regressions; Starlette #1012 referenced in code comments |
| Pre-auth IP limiter blocks legitimate clients sharing NAT (corporate) | Med | Med | Default 30/min/IP is permissive vs argon2 burn (30 attempts/min for malformed = ~30 × 50ms = 1.5s CPU/min — bounded); `TRUST_PROXY=true` when behind Caddy uses real client IP via X-Forwarded-For; tunable per-deploy |
| Concurrent counter TTL expires mid-stream (>60s stream) | High pre-fix | High | TTL refresh task (`_refresh_concurrent_ttl`) runs every 30s while request alive; integration test (90s stream) verifies cap holds |
| Redis exception leaves concurrent counter incremented | Med | High | Single `finally:` DECR pairs with the single Lua INCR (atomic); RedisError on subsequent operations propagates without further INCR; documented invariant |
| Streaming response: headers missed because route flushes early | Med | High | Headers passed to `EventSourceResponse(headers=...)` from route layer (read from `scope["state"]["rate_limit_headers"]`); send-wrap also injects on http.response.start as belt-and-suspenders for sync routes; integration test asserts headers in stream response BEFORE first data chunk |
| True-up race: response closed before background task runs | Low | Med | UsageTracking schedules INCRBYFLOAT on `http.response.body more=False`; uses phase-01 `_BG_TASKS` set; failures swallowed; PG INSERT idempotent |
| Tier cache stale after admin updates plans table | Low | Low | 5min TTL; `plans` table changes are rare; admin docs note "wait 5 min" |
| Redis down → all requests rejected (fail-closed) | Low | High | Decision: fail-OPEN on Redis error (log + allow); document trade-off — chosen because Redis outage = total platform outage anyway, no point doubly punishing |
| Token estimator wildly wrong for new model | Med | Med | True-up corrects within minute; alarm if `\|delta\| > 50% est` for > 1% of requests |
| Concurrent INCR without DECR (middleware exception before INCR) | Low | High | Lua atomic INCR+check; `try/finally` ensures DECR; 60s TTL backstop |
| ChatGPT account-level rate limit cascades into 429s confusingly | High | Med | Distinguish `account_rate_limited` (Codex error code) from wrapper `rate_limit_exceeded`; separate counter; surface to user as 503 not 429 (different retry strategy) |
| Reset header format mismatch with OpenAI SDK parser | Low | Med | Test against openai-python `RateLimitError.response.headers["x-ratelimit-reset-requests"]`; adjust format if SDK rejects |

## Security Considerations
- Per-API-key keys, NOT per-user (defends against single user with many keys bypassing limits — though tier is per-key by design).
- Lua scripts run server-side; no user input concatenated into Lua source — args passed via ARGV only.
- 429 error message MUST NOT leak other users' usage; only this key's remaining count is exposed.
- `Retry-After` capped at 3600s to prevent absurd values.
- `RATE_LIMIT_BYPASS=true` rejected in production environment (fail boot if `WRAPPER_ENV=prod` and bypass set).
- Monthly counter increments are AFTER successful 2xx response → 4xx attacks (auth fail, validation fail) cannot burn quota.

## Next Steps
- Phase 07 observability adds Grafana dashboards: rate_limit_rejections by dimension, top users by burn rate, monthly quota approach alerts at 80%/95%, plus `auth_rejections_total{reason='ip_pre_auth_429'} > 100/min` scan-detection alert.
- Phase 08 hardening: tier override per-API-key (priority customers), rate-limit bypass for healthcheck endpoints. **(Removed: per-IP fallback rate limit — now LIVE in this phase as `EdgeIPLimiter`, C2 fix.)**
- Billing integration (v1.1) consumes `usage_counter` rows directly; schema is forward-compatible.

## Unresolved Questions
1. **Reset header format exact**: `7m12s` vs `7m12.000s` vs ISO duration — need live OpenAI response capture for byte-fidelity. Phase 09 SDK compat test will surface this.
2. **(RESOLVED 2026-04-27 — defer to v1.1)** TPM per-window counter window-boundary unfairness: 2x burst possible across t=60s boundary. Decision: **accept as v1 known limitation** — internal-only scope reduces abuse vector; phase-10 load test will quantify actual impact. If post-deploy observed burst > 1.5x sustained, v1.1 switches to two-bucket interpolation (Cloudflare-style). Documented in plan.md Risks.
3. **(RESOLVED — C5)** Negative-cost ZSET entry approach is replaced with per-window `INCRBYFLOAT` counter. See Architecture §"TPM Lua script". The simpler "skip true-up, charge max_tokens" alternative is documented but NOT chosen by default; lead may flip via env flag if observed over-throttling rate is unacceptable.
4. **`TRUST_PROXY` setting** in dev (no Caddy): EdgeIPLimiter falls back to `scope["client"][0]` which is the local socket. Tests must set `TRUST_PROXY=true` and inject `X-Forwarded-For` to exercise per-IP behavior.
5. **EdgeIPLimiter and IPv6 /64 vs /128**: each IPv6 address is unique per-device; an attacker with /64 has 2^64 addresses. Should we group by /64 prefix? v1: full address (KISS); v1.1 if abuse observed: prefix-bucket.
