# Phase 06 Code Review — Multi-Tier Rate Limiting

Date: 2026-04-28
Reviewer: code-reviewer
Scope: 4 middlewares, 4 Lua scripts, 2 helpers, 1 migration, 4 test files.

## Verdict

**APPROVE_WITH_CHANGES** — architecture sound, atomic primitives correct, ordering verified, but two correctness gaps must be fixed before prod or quota tracking is non-functional.

## Critical (blocking)

### C1. UsageTrackingMiddleware reads `state["usage"]` but no route writes it
File: `src/gateway/middleware/usage_tracking.py:118`
- `_true_up()` does `usage = state.get("usage")` and only triggers TPM true-up + monthly Redis INCR + Postgres upsert when `usage` is truthy.
- `grep "request.state.usage" src/gateway` returns zero hits. `chat_completions.py`, `responses.py`, `jobs.py` never set it. Sync/stream handlers don't either.
- Effect on prod:
  1. TPM stays at the **upfront overestimate forever** — no negative-delta true-up. Effective TPM cap is roughly `tpm_estimate_per_call`, not `actual_tokens_per_minute`. Heavy users are rejected far earlier than their tier allows.
  2. `monthly:{user}:{period}` Redis counter is never incremented → monthly quota check at `rate_limit.py:175-181` always sees `cached_monthly is None` → quota never enforced.
  3. `usage_counter` Postgres table is never written → no analytics/billing source-of-truth.
- Fix: routes (or sync/stream handlers) must set `request.state.usage = {"input_tokens": …, "output_tokens": …, "total_tokens": …}` once Codex `token_count` is known. The middleware code already accepts `prompt_tokens`/`completion_tokens` aliases.

### C2. `peek_and_estimate` buffers unbounded request body
File: `src/gateway/rate_limit_token_estimator.py:122-134`
- Drains every `http.request` chunk into `body_chunks` with no byte cap; loops until `more_body=False`. A POST with `Transfer-Encoding: chunked` of arbitrary size will OOM the worker.
- This middleware runs after EdgeIP+Auth, so attacker needs a valid `cwk_` key — but a single compromised free-tier key (RPM=20, TPM=20k) can still send 20×1GB bodies/min and crash the pod before TPM check rejects (TPM check happens AFTER the buffer).
- Fix: add `max_body_bytes` setting (e.g. 1 MiB for chat/responses) and abort the loop with a 413 once exceeded; or short-circuit if `Content-Length` header > cap before reading.

## High

### H1. Concurrent counter not refunded on TPM/RPM rejection in middleware (it IS, via finally — but after extra round-trip)
File: `src/gateway/middleware/rate_limit.py:188-199, 292-308`
- Concurrent INCR happens before RPM/TPM checks. On RPM/TPM reject the `finally` DECRs — correct. Verified.
- But: when `concurrent_check` itself rejects (line 197-199) the `finally` block at 292 runs with `ok=False`, so DECR is skipped. That's correct (Lua already rolled back). Fine.
- Caveat: when `redis.RedisError` occurs in concurrent INCR (line 200-203), code sets `ok = True` (fail-open), then `finally` DECRs a key that was never INC'd → leaves counter at -1 if concurrent_key existed previously, eventually breaking the cap on recovery. Lua DECR returns the new value but no clamp at 0.
- Fix: track whether INCR actually succeeded; only DECR when it did. Or use Lua-clamped DECR (`max(0, INCR-1)`).

### H2. `_refresh_concurrent_ttl` task leaks settings/get_client refs across long stream
File: `src/gateway/middleware/rate_limit.py:310-327`
- The 30 s loop calls `get_client()` inside the body — fine.
- But `await _redis.expire(key, 60)` uses the literal `60` seconds while the rest of the file uses `_CONCURRENT_TTL_MS = 60_000` ms. Mixing units; works but inconsistent.
- More importantly the refresh loop catches `RedisError` once and exits. After a transient Redis blip the TTL refresh dies for the remainder of the stream — concurrent counter may expire, allowing over-cap requests on next call. Should retry with backoff.

### H3. EdgeIPLimiter `X-Forwarded-For` parsing trusts first hop without strip-list
File: `src/gateway/middleware/edge_ip_limiter.py:144-146`
- `xff.decode("latin-1").split(",")[0].strip()` takes the **leftmost** hop. Standard convention is leftmost = client (real source). OK if Caddy is configured to pass the original IP at front. But behind multiple proxies (CDN→LB→Caddy), an attacker can spoof XFF and bypass per-IP bucketing simply by setting `X-Forwarded-For: 1.2.3.4, …` — Caddy *appends*, doesn't replace.
- Fix: either strip Caddy's appended IP (use second-from-last hop) or document a strict deployment requirement that the entry proxy ALWAYS overwrites XFF with the real peer. Add to deploy guide.
- Less critical when `trust_proxy=False` (default) — uses `scope["client"]`. But spec defaults `TRUST_PROXY` for prod.

### H4. `peek_and_estimate` may break body parsing if request has no body
File: `src/gateway/rate_limit_token_estimator.py:125-132`
- `while more` initialises `more=True`, so it ALWAYS calls `receive()` once. For GET requests on `/v1/chat/completions` (which the path filter doesn't exclude — only path string match), this consumes the first ASGI message which might be `http.disconnect`, causing the route to hang awaiting body.
- Defense: route only accepts POST. But middleware doesn't check method. Adding `if scope.get("method") not in ("POST", "PUT", "PATCH"): return 0, receive` before draining is safer.

### H5. `_BG_TASKS` GC root never bounded — slow leak on traffic burst
File: `src/gateway/middleware/usage_tracking.py:48,99-103`
- Set grows when many in-flight true-ups happen. `add_done_callback(_BG_TASKS.discard)` runs on completion, but if Postgres pool is saturated tasks accumulate. Size could grow into tens of thousands during incident → memory pressure.
- Fix: cap with a semaphore (e.g. 100 concurrent true-ups), drop excess and log `usage_tracking.dropped`.

## Medium

### M1. Tier cache: thundering herd on miss
File: `src/db/crud/plans.py:73-91`
- N concurrent requests hitting expired cache all run `select(Plan)` simultaneously. With 200 RPS this is 200 simultaneous DB queries every 5 min.
- Fix: single-flight lock (`asyncio.Lock` per tier) or stale-while-revalidate.

### M2. Migration revision id is `"0004"` (string) but Phase 06 spec mentions `0004_plans` AND `0005_usage_counter` as separate migrations
File: `src/db/migrations/versions/20260427_0004_plans_seed.py:24,56`
- Spec asked for two migrations; impl combined into one. Acceptable refactor (KISS), but confirms only via a code review of the spec. Down-migration order (`drop_table("usage_counter")` then `drop_table("plans")`) is correct (no FK between them).

### M3. EdgeIPLimiter doesn't skip `/admin/*` or `/docs/*`
File: `src/gateway/middleware/edge_ip_limiter.py:41`
- Admin routes use `X-Admin-Token`, no Bearer. They will fail the `_BEARER_RE` match and be IP-bucketed. With `IP_PRE_AUTH_RPM=30` an admin running an automation script gets blocked.
- Fix: add `/admin/`, `/docs/`, `/openapi.json` to skip set, OR widen the bearer regex to also accept `X-Admin-Token` presence as bypass.

### M4. UsageTracking belt-and-suspenders monthly check is missing
File: `src/gateway/middleware/usage_tracking.py:18-19` (docstring claim)
- Docstring says "UsageTracking is the authoritative final check since it runs closest to the route" but the code never re-checks monthly quota before the route runs (it only runs `_true_up` AFTER response). Docstring is misleading.

### M5. TPM rejection path doesn't refund the upfront charge in case of route exception
- TPM Lua charges ARGV[4] cost upfront. If `await self.app(...)` (line 290) raises before completion, the `finally` (line 292) only refunds concurrent. The TPM charge stays. True-up never runs (UsageTracking only schedules true-up on `status<400`). Cumulative effect: route 500s leak budget.
- Fix: in `finally`, if no true-up was scheduled, INCRBYFLOAT a negative delta of the full estimate.

## Low / Nitpicks

- L1. `concurrent_check.lua:24` rolls back via DECR — if PEXPIRE keeps refreshing on rejected calls (line 20), an attacker spamming over-cap requests pins the TTL forever. Minor; counter never grows.
- L2. `sliding-window.lua:48` returns `window_ms` as `reset_ms` on the accept path — that's the FULL window TTL, not time until oldest entry expires. OpenAI's `X-RateLimit-Reset-Requests` should reflect when capacity actually frees. On reject path (line 37) it computes correctly; on accept it lies.
- L3. `tpm_check.lua:46` sets `PEXPIRE window_ms * 2` (=120 s) — fine for crossing window boundary, but TTL refresh on each call means counter from window N can persist into window N+2 if traffic is steady. Inline `eval` in usage_tracking also re-extends to 120 s. Bounded harmless drift.
- L4. `rate_limit.py:228` passes `str(uuid.uuid4())` as RPM entry_id — 36-byte ZSET member. With 200 RPM/key/min × 24 h that's 200×60×24×36 = 10 MB per key per day. Eviction handled by ZREMRANGEBYSCORE so OK.
- L5. `format_reset(0)` returns `"0s"` — OpenAI typically returns at least `"1ms"` for sub-second resets. Minor SDK compat.
- L6. `usage_tracking.py:131-140` uses inline Lua via `redis.eval` rather than registering a script. Two extra round-trips for SHA caching not exploited. Style only.
- L7. `rate_limit.py:174` constructs `period = _month_start_utc()` per request — small; cache it on scope.
- L8. `peek_and_estimate` writes `_body_bytes` on scope state but no route uses it. Dead state.

## Spec Adherence

| Spec item | Status |
|-----------|--------|
| Middleware order EdgeIP → Auth → RL → Usage | OK (test_middleware_execution_order verifies) |
| Raw ASGI (no BaseHTTPMiddleware) | OK across all 4 middlewares |
| C2: pre-auth IP bucket before argon2 | OK (`edge_ip_limiter.py:90-123`) |
| C3: streaming bypass middleware buffer | OK (raw ASGI + send-wrapper, not call_next) |
| C4: PEXPIRE refresh every call | OK (`concurrent_check.lua:20`) |
| C5: TPM via INCRBYFLOAT, not negative ZSET | OK (`tpm_check.lua`) |
| Reset header format `<m>m<ss>s` | OK (`rate_limit_reset_format.py`) |
| Monthly quota enforcement | **BROKEN** (C1) — never increments |
| TPM true-up | **BROKEN** (C1) — never runs |
| Postgres usage_counter writes | **BROKEN** (C1) — never runs |
| Tier cache 5-min TTL | OK |
| Fail-open on Redis error | OK across all middlewares |

## Strengths

- Atomic Lua scripts are textbook-clean: TOCTOU-free, idempotent, handle missing-key cases via `tonumber(... or '0')`.
- Middleware ordering test (`test_middleware_execution_order.py`) directly asserts the LIFO mental model — protects against future regressions.
- Lazy script load + `register_script` exploits EVALSHA fast path correctly.
- `send_429` constructs ASGI bytes directly — avoids the Starlette JSONResponse pitfall in raw ASGI.
- `State` propagation between Auth (writes `request.state.x`) and RL/Usage (reads `scope["state"]`) verified through Starlette `State.__setattr__` → `scope["state"]` dict. Compatible.
- Migration combines plans+usage_counter cleanly; defaults via `server_default=text("0")` allow re-running without explicit values.
- Down-migration drop order respects no-FK-between-them.
- Test coverage spans Lua atomicity, middleware order, reset format, edge IP, concurrent release.

## Unresolved Questions

1. Where exactly should `request.state.usage` be set — at end of sync handler (`handle_sync`) and at SSE-final-chunk in stream handler? Both? This is a routing concern that needs alignment with phases 03/04 owners.
2. Is the EdgeIP `/admin/*` skip a deliberate omission (admins are presumed trusted IPs)? If yes, document. If no, fix M3.
3. Does prod deployment ALWAYS have Caddy strip incoming X-Forwarded-For? Confirm in deploy guide before relying on first-hop XFF (H3).
4. Should Phase 06 introduce a `RATE_LIMIT_FAIL_OPEN` setting flag (default True)? Currently fail-open is hard-coded. Trade-off: Redis outage → DoS protection bypassed.

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical count:** 2
