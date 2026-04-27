# Red Team Review — Codex Wrapper Plan

**Reviewer:** code-reviewer (adversarial mode)
**Date:** 2026-04-27
**Target:** plans/260427-1358-codex-openai-wrapper/ (plan.md + 11 phase files)
**Posture:** Hostile. Find what survives CI but breaks in prod.

---

## Verdict
**ACCEPT_WITH_CHANGES**

The plan is unusually thorough for an 11-phase scope and the research grounding (researcher-01 + 02) is solid. Architectural shape is defensible, locked decisions are honest about risk, and most security primitives are present. **However**, several production-grade flaws are baked into the design that will surface as on-call pages within weeks of launch: (1) the rate-limit middleware ordering produces a fail-open auth-bypass DoS amplifier, (2) BaseHTTPMiddleware + StreamingResponse is a documented Starlette pitfall the plan is about to step on for SSE, (3) concurrent-counter DECR is missing from at least three early-return paths, (4) the Lua "negative-cost ZSET entry" for TPM true-up is unsound math against ZRANGEBYSCORE eviction, (5) the audit log writes from a fire-and-forget task that creates its own session — leaking connections under load, (6) `--ephemeral` is referenced in phases 02–03 but is **not in the Codex 0.125.0 documented flag list** in researcher-01 §4 (only `exec resume` references session persistence), and (7) the entire product hinges on a single ChatGPT login whose ToS prohibition is hand-waved as "disclose to users" — that's a **legal-risk landmine**, not a mitigation. Biggest single worry: the Codex 0.125.0 flag set is going to bite phase 02 hard, and there's no plan to verify the flags before phase 02 begins coding.

---

## Critical Issues (must fix before implementation)

### C1. `--ephemeral` flag may not exist in Codex 0.125.0 — plan references it as if confirmed
**Phase ref:** 02 step 5 (line 247 of phase-02), 03 §Key Insights bullet 4 (line 22), 03 step 7
**Offending line:** `argv = [..., "--ephemeral", "--skip-git-repo-check", ...]`
**Problem:** Researcher-01 §4 lists `codex exec --ephemeral "task"` under "Execution Mode Flags" but the same researcher §8 says "If `--ephemeral`, session is lost" — sourced from synthesis, **not** from a verified flag dump. The actual Codex 0.125.0 binary may not accept `--ephemeral`; if it doesn't, every chat-completions request fails with `unrecognized argument: --ephemeral` AND we don't notice until phase 02 is wired into a real binary in phase 09. CI compat tests use mock-codex (phase 09 §Architecture), so they don't exercise the real flag.
**Fix:** Phase 00 acceptance criterion already includes `docker compose exec gateway codex --version` — extend to `codex exec --help | grep -E "ephemeral|skip-git-repo-check"` and **fail bootstrap** if missing. If absent, redesign chat path to use `--cd` + ephemeral tmpdir alone (no session persistence flag), or skip `codex exec resume` workflows entirely.

### C2. Middleware ordering creates auth-bypass DoS amplifier when AuthMiddleware errors
**Phase ref:** 06 step 10 (`app.py` middleware registration), cross-ref phase 01 step 11
**Offending line:** `app.add_middleware(UsageTrackingMiddleware) / app.add_middleware(RateLimitMiddleware) / app.add_middleware(AuthMiddleware)` (FastAPI executes last-added FIRST)
**Problem:** Phase 06 acknowledges the order — auth THEN rate-limit. That's correct intent. **But** phase 01 step 8 says "On any internal exception → 500 generic OpenAI shape (don't leak)" — meaning any DB hiccup during argon2 lookup returns 500 **without** going through rate-limit. **Worse:** phase 06 step 7 (`if not getattr(request.state, 'api_key_id', None): return await call_next(request)` — fall-through if auth missing). An attacker spamming malformed auth headers passes auth middleware (which 401s) but those 401s also pass through with NO rate-limit gate. Free DDoS on argon2 verify (~30ms each = ~33 req/s burns one CPU forever).
**Fix:** Add a **per-IP coarse rate limit** (Caddy edge limit at 1000/min/IP per phase 10 is too generous for argon2-bound auth). Implement a fast pre-auth limiter on `Authorization` header presence — if header missing/malformed, increment IP-keyed bucket and reject 429 BEFORE argon2. Document explicitly that phase 06 limiter does NOT cover unauthenticated requests; the IP fallback is mandatory for v1, not "phase 08 future work" (phase 08 step 11 mentions "per-IP fallback rate limit" as defer).

### C3. SSE streaming via BaseHTTPMiddleware is a documented Starlette deadlock/buffering hazard
**Phase ref:** 03 step 7 (`stream_chunks` returned via StreamingResponse), 06 step 7 (`UsageTrackingMiddleware(BaseHTTPMiddleware)`), 04 step 5 (EventSourceResponse)
**Offending lines:** Phase 06: `class UsageTrackingMiddleware(BaseHTTPMiddleware)`, Phase 06 §Key Insights "Streaming responses + headers ... usage_tracking is outermost middleware injects pre-stream snapshot"
**Problem:** Starlette's `BaseHTTPMiddleware` is known to break or buffer streaming responses (issue starlette#1012, fastapi#5536 — well-documented). When `BaseHTTPMiddleware.dispatch` returns `response`, Starlette consumes the body iterator into memory **unless** you carefully forward the StreamingResponse. The plan's `UsageTrackingMiddleware.dispatch` does `response = await call_next(request); response.headers[...] = ...; return response` — this works for JSON but **silently buffers SSE**, killing the entire streaming UX. The "p95 first-token < 2s" metric (brainstorm §9) becomes unachievable. The integration test "Streaming response: headers present BEFORE first SSE chunk byte" (phase 06 success criteria) WILL pass against TestClient (which buffers anyway) and FAIL behind real Caddy/uvicorn.
**Fix:** Drop `BaseHTTPMiddleware` for any middleware in the SSE path. Use raw ASGI middleware (`async def __call__(self, scope, receive, send)`) that wraps `send` to inspect first message and inject headers, OR move header injection into the `StreamingResponse` constructor (set headers in route handler before returning), OR use `add_middleware` with proper ASGI class. Phase 06 needs a rewrite; the BackgroundTask true-up logic mostly survives but header injection moves into the route layer. **Test gate:** add a real-uvicorn integration test that proxies through nginx-with-buffering-on (simulating misconfigured proxy) and asserts first byte arrives within 1s of subprocess first event.

### C4. Concurrent counter has at least three uncovered DECR paths → permanent lockout
**Phase ref:** 06 step 7 (rate_limit middleware finally-block)
**Offending line:** `try: ... return await call_next(request); finally: await self.redis.decr(f"concurrent:{key_id}")`
**Problem:** Multiple paths skip the INCR-DECR pairing:
  1. **Concurrent-check failure path** (step 7 line "if not ok: return self._reject(...)"): this returns BEFORE entering the try — `_concurrent_lua` already INCRed and conditionally DECRed inside Lua per phase 06 §"Concurrent counter race". OK. But: subsequent RPM/TPM checks INCR concurrent (already done) but if RPM rejects, the `finally` DECRs — good. EXCEPT the early-return path (`return self._reject("rpm_exceeded")`) inside the `try:` means `finally` DOES run. OK actually. **Hidden issue:** if Redis `_sw_lua` raises (timeout, connection error), the `finally` runs but Lua INCR may have happened atomically while DECR fails → counter drift up.
  2. **Worker process killed (SIGKILL by orchestrator) mid-request:** TTL 60s saves us — but stream durations exceed 60s routinely. Counter resets WHILE request still running → next concurrent INCR sees 1 instead of 2 → cap exceeded silently.
  3. **`UsageTrackingMiddleware` raises during header injection** (e.g., `format_reset_ms` bug): `RateLimitMiddleware`'s `finally` did its DECR — but only if `UsageTracking` is OUTSIDE `RateLimit` in the ASGI chain. Phase 06 step 10 says "FastAPI executes last-added first" then registers `UsageTracking` first, `RateLimit` second, `Auth` third — meaning execution order on REQUEST is Auth → RateLimit → UsageTracking. On RESPONSE, reversed. So `UsageTracking.dispatch` wraps `RateLimit.dispatch` — meaning `RateLimit.finally` runs INSIDE `UsageTracking.dispatch`, and a `UsageTracking` post-call exception happens AFTER `RateLimit` finished. OK for that case. **But the `BackgroundTask` true-up runs after response close**, after `RateLimit.finally` — true-up exceptions don't affect concurrent. OK.
**Net:** path 1 (Redis flake during sliding-window) and path 2 (long stream + 60s TTL) are real bugs.
**Fix:** Make TTL refresh on every check (PEXPIRE in Lua); add explicit DECR in exception handler around `_sw_lua` calls; document the 60s TTL as "concurrent counter MUST recover within 60s of last request activity for that key". Add an integration test: spawn a stream lasting 90s while making 1 other concurrent request → should still see correct cap enforcement.

### C5. Lua "negative-cost ZSET entry" for TPM true-up is mathematically wrong against window eviction
**Phase ref:** 06 step 8 (`_sw_correction`), 06 §Architecture "For TPM, encode cost in the entry value", 06 §Unresolved Q3
**Offending line:** "For negative delta: ZADD with cost-encoded entry value containing negative weight; window sum honors negatives"
**Problem:** Sliding window via ZSET works because `ZREMRANGEBYSCORE 0 (now-window)` evicts old entries. With **negative-cost entries**, evicting a negative entry from the window INCREASES the sum (you wanted to subtract; eviction takes that subtraction away). Concretely: estimate=1000, actual=500, you ZADD a -500 entry at `now`. 60s later that -500 entry expires → sum jumps back up by 500 → TPM appears to spike retroactively. The estimate=1000 entry also expires at the same time, so net is correct AT THAT INSTANT — but at any time strictly between `now+ε` and `now+60s`, the user spent 500 tokens but the window says 500. After eviction, both gone, window says 0. Looks fine. **But:** if user makes ANOTHER request at `now+30s` with estimate=1000 → window now reads (1000 + (-500) + 1000) = 1500 even though the 1000+(-500) pair represents a SETTLED 500. This is correct! Wait — let me re-check. Actually it IS correct... over the full window. **Real bug:** when `ZRANGE+sum` runs, it reads ALL entries in the window. If the negative entry has score=`now` and the original positive entry has score=`(now - duration_of_request)` where duration > window/2, the original may evict before the negative does → window sum drops below truth → user gets free TPM until the negative also evicts.
Concretely: 60s window, request started at t=0 with estimate=1000, request ended at t=70 (longer than window!), true-up writes -500 at t=70. The original 1000 at t=0 was already evicted at t=60. Now from t=70 onwards the window has -500 with no offsetting positive → user has -500 in window → can spend 500 ABOVE limit before another request. Free quota stealing.
**Fix:** Don't use negative-cost entries. Use the alternative documented in unresolved Q3: maintain parallel counter with EXPIRE for the actual sum, and use ZSET only as a rate-of-additions tracker. Or: use a fixed window with reset-at-minute-boundary (simpler, less accurate but bounded). Or: skip true-up entirely — overcharge based on max_tokens estimate and accept the slight over-throttling (more honest, easier to reason about).

### C6. Path traversal `validate_path_inside` symlink check has TOCTOU race
**Phase ref:** 02 step 4
**Offending line:**
```python
for part in target.relative_to(workspace).parts ...
    p = p / part
    if p.is_symlink(): raise WorkspaceTraversalError(...)
```
**Problem:** Walks the parent components checking for symlinks before resolving — but between the check and the actual write, a malicious actor inside the sandbox (codex itself, executing user prompt's instructions) can `mv real_file symlink_to_etc_passwd; mv symlink_to_etc_passwd real_file` to swap. More fundamental issue: `target.relative_to(workspace)` on an **absolute** target outside workspace **raises ValueError** before the loop runs — the check `if target.is_absolute() else (target,).parts` fallback is wrong (turns the whole path string into a single "part"). Test case `target = Path("/etc/passwd")` → `target.relative_to(workspace)` raises → exception propagates as ValueError, not WorkspaceTraversalError — caller may not catch.
**Fix:** Realpath resolution before the check, not after walking parts. Use `os.path.realpath(target)` (single resolution), then assert `os.path.commonpath([resolved, workspace_resolved]) == workspace_resolved`. For symlink-during-write: rely on Landlock (Linux) / Seatbelt (macOS) to enforce — application-layer check is best-effort defense-in-depth and should not be the primary control. Document that the validate function is not race-free; the sandbox is.

### C7. ChatGPT login as the sole upstream is a legal/business landmine, not just a HIGH risk
**Phase ref:** brainstorm §7 first row, plan.md §Risks
**Offending line:** "ChatGPT account ban for API resell ... HIGH ... (a) Privacy policy + ToS disclosure to users"
**Problem:** OpenAI's ChatGPT Terms of Service explicitly prohibit using ChatGPT to build competing products or to programmatically access ChatGPT for resale (last seen: section 2 use restrictions, "you may not ... use output ... to develop models that compete with OpenAI" and "use any automated or programmatic method to ... access the Services"). Codex CLI uses the ChatGPT plan via the same auth surface. **Disclosing this to users does not legalize it** — users disclosing they violate ToS doesn't prevent the wrapper operator's account ban or potential breach-of-contract liability. Worse: phase 09 success metric "99.5% uptime / 30d" is impossible if OpenAI bans the account at any point in the period. "Single account v1" means **one ban event = total platform failure with no failover**.
**Fix:** This is a product decision, not an engineering one. Lead must decide: (a) ship as internal/dev tool only (no external paying users), (b) switch to API-key path before launching to anyone (uses CODEX_API_KEY env per researcher-01 §5 "Additional findings") — cost goes up but legal risk drops, (c) get explicit written legal review and risk-accept. Engineering plan should NOT promise 99.5% uptime if option (a) or (c) is chosen. **Do not ship to paying customers under option (a).**

### C8. `audit_log.emit` fire-and-forget creates orphan DB sessions and leaks task references
**Phase ref:** 01 step 6 (`update_last_used_fire_and_forget`), 08 step 2 (`audit_log.emit ... asyncio.create_task(_persist(fields))`)
**Offending line:** "schedules a coroutine via `asyncio.create_task`; uses a fresh session"
**Problem:** Two distinct issues:
  1. **Task reference leak:** `asyncio.create_task` without storing the task in a set leads to "Task was destroyed but it is pending!" warnings, garbage-collected mid-execution under memory pressure → silent audit log loss.
  2. **Session pool exhaustion:** Each fire-and-forget gets its own session from the factory. Phase 00 sets pool_size=10. Under 100 RPS with 50ms argon2, you have ~5 in-flight requests + 5 background last_used_at writes + audit log writes = pool exhaustion → `last_used_at` updates start failing → next requests block on pool acquire → cascading latency. The "wrap in try/except, log at WARN" mitigation in phase 01 risk table catches DB errors but **doesn't address the CONNECTION ACQUISITION timeout** that comes first.
**Fix:** (a) Maintain a global `_BG_TASKS: set[asyncio.Task]` set; `task = asyncio.create_task(...); _BG_TASKS.add(task); task.add_done_callback(_BG_TASKS.discard)`. (b) Use a SEPARATE smaller dedicated pool for background writes (size 2-3) with `pool_timeout=0.5`; on timeout, log WARN and drop. (c) Consider in-memory queue (asyncio.Queue) consumed by a single background worker task — only ONE concurrent DB write for audit, never blows pool.

### C9. Postgres pool size 10 is dangerously low for the proposed concurrency
**Phase ref:** 00 step 7
**Offending line:** `create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=10)`
**Problem:** Each chat-completions request issues at minimum: (1) auth lookup (1 query), (2) rate-limit middleware tier lookup (cached but cold-miss = 1 query), (3) `last_used_at` update (1 query, fire-and-forget — see C8), (4) audit log emit (1 query, fire-and-forget), (5) handler — chat doesn't hit DB itself. Per request: 2-4 connections held briefly. At 100 RPS with 50ms p99 argon2, requests overlap → easily 10+ simultaneous. Default `pool_timeout=30s` means under burst, requests **wait 30s before timing out** with no useful error — looks like complete outage. Worker process has its own pool of 10. Compose runs single gateway uvicorn worker by default (no `--workers N`).
**Fix:** Bump `pool_size=20, max_overflow=10, pool_timeout=5` minimum. Document the math in code comment. Add metric `db_pool_acquire_seconds` histogram. Phase 07 already lists `db_pool_active`/`db_pool_idle` gauges — add the histogram. For multi-uvicorn-worker setup (production) the pool is per-worker so default of 4 workers × 30 = 120 pg connections; ensure `pg_hba.conf` and Postgres `max_connections` (default 100) is bumped — currently nothing in plan addresses this.

### C10. SSRF defenses on `repo_url` are incomplete; HEAD check follows redirects
**Phase ref:** 08 step 11
**Offending line:** `httpx.head(url, timeout=5, follow_redirects=True) → 200 expected`
**Problem:** `follow_redirects=True` to a github.com URL — but GitHub redirects 301 from `https://github.com/{user}/{repo}` to wherever they want, including potentially internal hosts in some attack vectors. Worse: even if redirects only go to github subdomains, an attacker who controls a repo can configure GitHub Pages CNAME tricks. The phase 08 `transport configured to refuse private IP redirects` is mentioned in security but there's no implementation guidance. `httpx` does NOT block private-IP-redirect by default.
**Fix:** `follow_redirects=False`; if HEAD returns 301/302, accept (GitHub's redirect to canonical URL is normal — but require Location to also match the regex). Or use a custom `httpx.AsyncHTTPTransport` with a DNS resolver that blocks RFC1918 + link-local + IPv6 ULA. Cite an example: https://github.com/encode/httpx/issues/761 community workarounds. Add unit test that supplies a `https://github.com/...` URL whose DNS resolves to 127.0.0.1 (mock) → request refused at transport layer.

### C11. Codex 0.125.0 changelog mentions "Unix socket transport" — current spawn approach may be outdated
**Phase ref:** 02, researcher-01 §6
**Offending line:** "0.125.0 | 2026-04-24 | Unix socket transport, reasoning-token usage, rollout tracing"
**Problem:** "Unix socket transport" suggests the CLI's preferred subprocess interaction may have moved away from stdout-pipe. If the subprocess now writes JSONL to a Unix socket by default and falls back to stdout only on `--legacy-stdout` or similar, the entire phase 02 design breaks. Researcher-01 didn't dig into this; no explicit verification that 0.125.0 still emits JSONL on stdout under `--json`.
**Fix:** Phase 00 acceptance: `echo "say hi" | codex exec --json --color never` produces JSONL on stdout. If not, raise immediately and revisit before phase 02. Add a one-paragraph note in phase 02 risk table: "Unix-socket transport could become default — verify pre-implementation."

---

## High Issues (should fix)

### H1. ChatCompletionChunk lacks `system_fingerprint` and `service_tier`
**Phase ref:** 03 step 2, researcher-02 §A.2
**Problem:** Researcher-02 §A.2 lists `system_fingerprint` and `service_tier` as optional but real OpenAI clients (especially newer ones) inspect `service_tier`. Plan's chat_response.py omits both. SDK won't fail — but compat tests asserting "byte-for-byte parity" (phase 09 §) will not catch the missing fields because openai-python tolerates extras. Document deviation explicitly. Optional but recommended: emit a static `system_fingerprint=f"codex-cli-0.125.0"`.

### H2. `finish_reason="error"` is not in the canonical OpenAI enum
**Phase ref:** 03 §Key Insights bullet 3
**Offending line:** "emit a final chunk with `finish_reason='error'`"
**Problem:** Researcher-02 §A.2 lists `stop|length|tool_calls|content_filter|function_call` — no `error`. Phase 03 risk table acknowledges "Documented deviation. Fallback: emit only `stop`". This isn't a documented deviation — it's a wire-format violation. openai-python's `ChatCompletionChunk` has `finish_reason: Optional[Literal["stop", "length", "tool_calls", "content_filter", "function_call"]]`. Strict validation will fail. Recently the SDK added `extra=allow` to chunk choices but the typing layer rejects unknown literals.
**Fix:** Use `finish_reason="stop"` and add an opaque chunk metadata field `_codex_error: {code, message}` (under `extra=allow`) for our own debugging. Strict.

### H3. Responses API `created_at` format mismatch — researcher-02 disagrees with itself
**Phase ref:** 04 step 1, researcher-02 §B.3.1
**Offending line:** `"created_at": "2026-04-27T10:30:00Z"` (researcher-02 §B.3.1 ISO string) vs §A.5 chat-completions `created: 1704067200` (unix int)
**Problem:** Researcher-02 §B.3.1 shows ISO string, but openai-python's actual `Response` model uses `created_at: int` (unix ts). Phase 04 follows the researcher's bad fixture. SDK will reject ISO string with pydantic ValidationError.
**Fix:** Verify against openai-python source: `from openai.types.responses import Response` → check `created_at` annotation. If int, fix phase 04. (Strong prior: it IS int based on convention.) Add to phase 09 a deliberate check.

### H4. Sequence number race in Responses emitter when stream cancelled mid-emission
**Phase ref:** 04 step 2 ("self.seq += 1")
**Problem:** `_emit` increments `self.seq` AFTER yielding. If yielded event raises during JSON serialize (rare), seq stays at old value but event was returned to client (or partially flushed). Client now sees seq jump by 2 on next event. Emitter design is "increment after successful yield" but yielding is the act of returning a tuple — the I/O happens in the `_stream` generator further out. If `EventSourceResponse.send` fails after our yield, seq has already advanced.
**Fix:** Increment AFTER successful network flush is impossible from inside the emitter. Acceptable trade-off but document the gap. Test: kill connection mid-stream, verify next request's emitter starts fresh seq=0 (one emitter per request — confirmed in phase 04 architecture).

### H5. `--ask-for-approval never` paired with `workspace-write` for jobs is risky absent strict prompt control
**Phase ref:** 02 step 5, 05 step 9
**Problem:** Phase 02 §Security says "NEVER pair with `danger-full-access`" — agreed. But `workspace-write` + `never` means codex auto-approves any file write inside the workspace, including writing into `.git/hooks/` of the cloned repo. If codex installs a malicious hook (prompt-injected by user task or by repo content), then any subsequent `git -C ... diff` (phase 05 step 11) **runs that hook** under worker user. Defense-in-depth: also block hooks via env or git config.
**Fix:** Set `git config --global core.hooksPath /dev/null` in worker container before clone. Or use `git -c core.hooksPath=/dev/null` on every git invocation. Document.

### H6. `git clone --depth 1 -b {branch}` rejects non-branch refs (tags, SHAs)
**Phase ref:** 05 step 10
**Problem:** The schema validates `branch: str = "main"`, doesn't constrain format. Users will pass tag names (`v1.2.3`), commit SHAs, or HEAD-relative refs. `--depth 1 -b` requires a branch or annotated tag; SHAs fail. Error message is confusing ("fatal: Remote branch abc1234 not found in upstream origin").
**Fix:** Either restrict the schema to alphanumeric branch names with explicit allowlist regex, OR fall back to `git clone --depth 1` then `git checkout <ref>`. Phase 08 step 11 mentions branch-name regex `^[A-Za-z0-9._/-]{1,200}$` — accept SHA via separate field or relax.

### H7. Workspace tmpfs sized 10GB doesn't survive 5 concurrent monorepo clones
**Phase ref:** 10 step 6
**Offending line:** `tmpfs ... size=10g`
**Problem:** Free tier concurrent=2, pro=10, ent=50 (phase 06). At pro tier with 5 concurrent jobs cloning typical-sized repos (Linux kernel = 4GB shallow, large monorepos commonly 2-3GB), tmpfs OOMs immediately. Worker doesn't gracefully handle ENOSPC — `git clone` fails, error message leaks workspace path to user.
**Fix:** (a) Reduce concurrent caps for jobs specifically (separate from chat concurrent), (b) add per-job repo size pre-flight (`git ls-remote --heads` + GitHub API content-length probe), (c) bump tmpfs to 50GB, (d) move workspaces from tmpfs to a regular volume (slower clones but bounded by disk, easier to reason about). Phase 10 risk table doesn't even list "tmpfs OOM" — should be top-3.

### H8. Argon2 verify per request → 100 RPS = ~5 CPUs at 50ms each
**Phase ref:** 01 §Key Insights bullet 1
**Offending line:** "for high RPS we cache `(token_prefix → user_id)` in Redis with short TTL — KISS: defer cache to phase 6 unless benchmark shows pain"
**Problem:** Argon2id default params (m=64MiB, t=3, p=4) take ~30-100ms per verify on commodity hardware. At free-tier 20 RPM × 100 keys × concurrent burst, you can saturate one core. Bigger issue: argon2 m=64MiB means 100 concurrent verifies = 6.4GB RAM. Default uvicorn workers × concurrent verifies × 64MB → blows memory budget. The "defer cache to phase 6" is a plan trap because phase 6's rate limit comes AFTER auth in middleware — meaning the limiter doesn't help with auth cost. By phase 6 there's no cheap way to add the cache without re-architecting.
**Fix:** Build the cache in phase 01, not phase 06. Cache `(plaintext-key-sha256-prefix → cached_api_key_id)` in Redis with 5-minute TTL. Verify argon2 only on cache miss. Documented trade-off: revoking a key takes up to 5 min to take effect (vs current "immediate"). For the 24h grace rotation window (phase 08) this is fine. Alternative: lower argon2 params to t=2, m=16MiB for an internal-only API gateway (acceptable — your threat model is "DB exfil" + "we still want offline crack to be expensive", not "we host nation-state-target login"). Document the parameter choice.

### H9. `request.is_disconnected()` polling is expensive and racy
**Phase ref:** 08 step 5
**Offending line:** "every yield iteration check `await request.is_disconnected()`"
**Problem:** `Request.is_disconnected()` reads from the ASGI receive channel. Calling it inside `async for evt in run_codex(...)` between yields blocks the event loop briefly per event — measurable at high event rate. Worse: it's not race-free; client may have disconnected after the check but before the next yield, response writer raises ClientDisconnect.
**Fix:** Two patterns work: (a) launch a watcher task `asyncio.create_task(_watch_disconnect(request, cancel_event))` that polls less often (1s) and sets an event; runner checks event each yield (cheap). (b) wrap the StreamingResponse iterator in a try/except that catches `ConnectionError`/`ClientDisconnect` and triggers cleanup. Don't poll `is_disconnected` per yield.

### H10. Mock-codex never tests the "MCP stdout contamination" guard
**Phase ref:** 09 §Architecture, fixtures
**Problem:** Researcher-01 issue #15451 says MCP tool output can leak to stdout. Phase 02 has `parse_line` defensive guard. Phase 09 fixtures (`happy-path.jsonl` etc) emit clean JSONL — no contamination. The guard is never exercised by compat tests. If the guard regresses (someone "simplifies" to remove the prefix check), tests pass; production breaks.
**Fix:** Add a fixture `contaminated.jsonl` that mixes plain text lines (`Connected to mcp_server foo...`) with JSONL events. Assert wrapper still produces a valid completion, ignoring the noise.

### H11. Diff blob 16 MB cap stored in Postgres TEXT column → bloated query plans
**Phase ref:** 05 step 11
**Problem:** `jobs.diff_blob TEXT` with rows up to 16 MB. Postgres TOAST handles this but `SELECT *` on jobs table (e.g., admin list endpoint, observability) drags those into memory. Worse: pg_dump backups balloon proportionally.
**Fix:** Move diff_blob to S3/MinIO from day 1, store presigned URL or object key in `jobs.diff_blob_url`. Phase 08 mentions this as deferred — bring it forward. If keeping in DB, store in a SEPARATE `job_diffs` table with FK so listing/searching jobs doesn't touch blob columns.

### H12. Audit log retention purge has no row-count cap → unbounded delete-by-timestamp lock
**Phase ref:** 08 step 14
**Offending line:** `DELETE FROM audit_log WHERE created_at < now() - interval '<retention> days'`
**Problem:** After 90 days at 100 RPS = ~777M rows. DELETE without LIMIT or batching takes a long-running ACCESS EXCLUSIVE table lock on Postgres, blocks all writes, may time out. Vacuum fallback won't reclaim space without explicit VACUUM FULL.
**Fix:** Implement batched delete: `DELETE FROM audit_log WHERE id IN (SELECT id FROM audit_log WHERE created_at < $1 LIMIT 10000)` in a loop with sleeps. Or use partitioned tables (monthly partitions, DROP PARTITION is instant). At expected scale, partitioning is the right call.

### H13. "Tier change while user has in-flight requests" not addressed
**Phase ref:** 06 step 5 (5min cache)
**Problem:** Tier limits cached in-process for 5 min. If admin downgrades user (free → suspended-tier), in-flight requests continue under old limits; new requests within 5min still see old limits. For abuse response (one user spamming), 5 min is too slow.
**Fix:** Either drop in-process cache (fetch from Redis on every request — Redis is fast enough), or add a Redis pub/sub channel `tier:invalidate:{user_id}` that admin endpoint publishes on tier change; subscribers drop their cache entry. Document either.

### H14. `cancel:job:{id}` Redis key with 5min TTL races against worker boot lag
**Phase ref:** 05 step 6 + risk table row "Cancel flag TTL expires before worker reads"
**Problem:** Phase 05 risk table acknowledges this but mitigation says "TTL 300s vs poll interval 1s; worst case worker misses cancel for queued jobs only". For QUEUED jobs that have been queued >300s (Arq backed up), cancel flag is already gone when worker picks it up → user thinks cancelled, worker runs anyway, charged for tokens.
**Fix:** Either (a) make cancel atomic with status update in DB: API DELETE sets `jobs.cancelled_at` AND publishes Redis flag; worker checks DB row at start, not just Redis. (b) Increase TTL to 24h. (c) Bind cancel to job.created_at: TTL = `JOB_TIMEOUT_SECONDS + 60`.

### H15. `EventSourceResponse` from sse-starlette adds `event: ping` keepalives by default — and `[DONE]` is auto-injected
**Phase ref:** 04 step 5
**Offending line:** "sse-starlette ... Verify it does NOT inject `[DONE]` (it doesn't by default)"
**Problem:** sse-starlette has `EventSourceResponse(ping=15)` default, sending `: ping` comments every 15s (this is fine, OpenAI does similar). **But** different versions have different keepalive formats; some emit empty events that confuse strict consumers. Also: `EventSourceResponse` may add a final empty event on close. The phase 04 plan asserts "no `[DONE]`" but doesn't explicitly disable keepalive.
**Fix:** Pin `sse-starlette` version, configure `ping=None` if not needed (responses API has no keepalive doc'd), or `ping=15, ping_message_factory=lambda: ...` for explicit control. Add raw-byte test: stream lasting 30s asserts the only `event:` lines emitted are the 4 lifecycle events + N delta events.

---

## Medium Issues (consider)

### M1. Phase 04 reasoning items "deferred to phase 08" — but phase 08 doesn't mention them
**Phase ref:** 04 §Key Insights "Reasoning items deferred ... phase-08 hardening revisits"
**Problem:** Cross-reference broken. Phase 08 has zero mentions of `response.reasoning_summary_text`. Either bring forward to v1 or delete the deferral promise.
**Fix:** Either add a row to phase 08 §Implementation Steps, or document in v1.1 backlog. YAGNI argues delete the promise.

### M2. Phase 08 audit_log overlaps phase 07 logging — duplicated work
**Phase ref:** 07 §Required log fields, 08 §Audit log columns
**Problem:** Both maintain a record of every request. structlog logs go to stdout (phase 07) and audit_log goes to Postgres (phase 08). If both are kept, log shipper indexes the same data twice → cost. If audit_log is "the queryable one", structlog can be lighter (no `request_id` echo etc — but request_id is always logged anyway).
**Fix:** Pick one source of truth for "did request X happen". If structlog→Loki is queryable, skip audit_log table for `/v1/*` and only audit_log `/admin/*`. Phase 08 description literally says "Audit log row written for every `/v1/*` request" — that's a lot of rows for what amounts to log replication.

### M3. argon2id pepper missing — DB exfil makes offline cracking too cheap
**Phase ref:** 01 step 4
**Problem:** Plaintext key generation is `cwk_` + 32 bytes random = high entropy, so argon2 cracking is infeasible already. **But:** if attacker leaks DB AND learns the prefix scheme (cwk_), they can verify guesses against any key with the prefix. Adding a server-side pepper (HMAC-SHA256(plaintext, server_secret)) before argon2 means even with DB+prefix, cracking requires the server secret too.
**Fix:** Add `KEY_PEPPER` env (Pydantic SecretStr), HMAC-SHA256 plaintext before argon2 hash + verify. Document rotation procedure (any pepper change invalidates all keys — OK because admin can rotate).

### M4. Constant-time check on api_key match BEFORE argon2 has no shortcut
**Phase ref:** 01 step 6 `get_active_by_hash_match`
**Problem:** Logic is `SELECT WHERE prefix = ... AND revoked_at IS NULL`, then loop verify_key. If prefix is unknown (random hex), DB returns 0 rows immediately → fast path. If prefix matches but argon2 fails, 30ms cost. **Timing oracle:** known-prefix vs unknown-prefix have wildly different latencies. An attacker can enumerate prefix space (12 b64 chars = 2^72) — too large to brute, but they can detect "your prefix exists" by latency.
**Fix:** This is acceptable — 2^72 search space makes the oracle unactionable. But document the trade-off. If serious, run a dummy argon2 verify against a constant hash on prefix-miss to equalize timing.

### M5. Phase 07 says `service_tier` header but plan doesn't emit it — only researcher-02 mentions
**Phase ref:** 07 §Architecture, 03 §Architecture
**Problem:** Researcher-02 §A.2 lists `service_tier` as a top-level field in chunks. Plan ignores. SDK doesn't require it; not blocking. Tracking only.

### M6. Caddy `flush_interval -1` is required but not gated by integration test
**Phase ref:** 10 step 1, phase 03 risk table
**Problem:** Caddyfile.production has `flush_interval -1`. Phase 09 compat tests run AGAINST the gateway but NOT through Caddy. If someone removes the flush_interval, compat passes; production breaks.
**Fix:** Add a phase 10 staging smoke test that hits `https://staging.example.com/v1/chat/completions` stream and asserts first byte arrives < 2s.

### M7. OTEL trace context not propagated through Arq queue
**Phase ref:** 07 step 6
**Problem:** Auto-instrumentation lists FastAPI, asyncpg, redis, httpx, ArqInstrumentor (if available). ArqInstrumentor for OTEL exists but is third-party and may lag. Without it, gateway → enqueue → worker is a broken trace. Plan doesn't address.
**Fix:** Manual: enqueue stores `OTEL_TRACEPARENT` header in job kwargs; worker reads + sets it on the root span via `extract_context`. Phase 07 should add this explicit step.

### M8. `--no-install-recommends` + `npm install -g` doesn't pin npm registry
**Phase ref:** 00 step 13
**Problem:** Dockerfile installs `@openai/codex@0.125.0` from public npm. Reproducible only as long as that exact version stays available. Supply-chain attack vector (typosquat already happened in npm history; @openai org is gated but still relying on npm CDN).
**Fix:** Vendor the codex tarball into the Docker image (`npm pack @openai/codex@0.125.0`, commit `.tgz` or store in private artifact registry, `npm install -g ./codex.tgz`). Or add SHA verification. Cite Snyk supply-chain advisory.

### M9. Codex `auth status` retried "auth status" / "login status" but no "auth status --json"
**Phase ref:** 02 step 6
**Problem:** Subprocess parsing exit code only — doesn't verify the body. If `codex auth status` exits 0 but prints "warning: token will expire in 60 seconds", you don't catch upcoming expiry. 5-min poll interval means up to 5 min downtime when token finally expires.
**Fix:** Parse stdout for expiry hints, OR use phase 08's webhook to alert on `expires_at - now < 1h` from auth.json fallback parse.

### M10. Phase 09 coverage gate is per-module-AGGREGATE, not per-file
**Phase ref:** 09 §Key Insights
**Offending line:** "Coverage gate ≥ 75% on `src/gateway`, `src/codex`, `src/workers`"
**Problem:** Aggregate 75% on a directory means a 99%-covered settings.py + 50%-covered runner.py = 75%. The 50% module is the one that matters.
**Fix:** Set per-file floor: pytest-cov supports `--cov-fail-under=75` aggregate; for per-file use `coverage report --fail-under=70` on each module. Or list explicit critical files (runner.py, jsonl_parser.py, stream_handler.py) with floor 90%.

### M11. Phase 06 test `test_rate_limit_429.py` "passes within 5s (no flake)" is wishful
**Phase ref:** 06 success criteria
**Problem:** Sliding window tests are notoriously flaky in CI due to clock jitter. 5s wall budget for "burst 21 requests + assert 429 + check headers" is tight; CI runners under load can take longer.
**Fix:** Use freezegun or mock `now_ms` argument to Lua script. Wall-clock tests defer to staging.

### M12. Phase 05 worker `last_msg = ""` only captures LAST agent_message, drops others
**Phase ref:** 05 step 9
**Offending line:** `if evt.get("type") == "item.completed" and evt["item"].get("type") == "agent_message": last_msg = evt["item"]["text"]`
**Problem:** Codex emits multiple agent_messages per turn (researcher-01 §2). Job summary loses all but last. User who gave a multi-step task sees "I'm done" as summary, not the actual changes.
**Fix:** Append to a list, join with `\n\n` for summary. OR pick longest. Document choice.

### M13. JSON Lines decoder `errors="replace"` substitutes U+FFFD silently
**Phase ref:** 02 step 5
**Offending line:** `line = raw.decode("utf-8", errors="replace")`
**Problem:** Bad-encoding bytes become U+FFFD, then `json.loads` parses... whatever. Unicode injection from prompt could put U+FFFD in output, which then gets decoded incorrectly. Edge case: `errors="strict"` and catch UnicodeDecodeError → log + skip line is more honest.
**Fix:** Trade off; minor.

### M14. `WORKSPACE_ROOT` host bind vs container path reconciliation untested
**Phase ref:** 00 + 02 + 05
**Problem:** Phases reference `/workspaces/{job_id}` absolute path inside container, but compose file isn't shown to confirm volume mount. tmpfs in phase 10 — but phases 00/02/05 don't pin the volume type. If host runs Linux 5.13 (Landlock requires this) but the volume is a host-bind into ext4 with default opts, AppArmor/SELinux interactions can deny ops codex tries.
**Fix:** Phase 00 Dockerfile/compose explicitly mounts `/workspaces` as `tmpfs` (or volume) with `noexec,nosuid` flags. Phase 10 tmpfs config should already exist in phase 00 dev compose for parity.

### M15. Phase 08 admin rotation: `replaces_id` column not in phase 01 schema
**Phase ref:** 01 step 2 vs 08 step 9
**Problem:** Phase 01 ApiKey ORM model doesn't include `replaces_id` FK. Phase 08 introduces rotation that needs it. Means another migration. OK but the chain `001 → 002 (jobs) → 003 (plans+usage_counter) → 004 (audit_log extend) → 005 (api_keys.replaces_id)` should be planned.
**Fix:** Add `replaces_id` to phase 01 schema upfront. Adding columns is cheap; missing them is a phase-008 rewrite.

### M16. `Dockerfile.gateway` UV install via pip → no cache layer
**Phase ref:** 00 step 13
**Problem:** `RUN pip install uv && uv sync --frozen --no-dev` runs after `COPY pyproject.toml uv.lock ./`. If pyproject changes, full rebuild. KISS but slow.
**Fix:** Multi-stage build: builder stage with uv + sync, runtime stage copies `.venv`. Keeps image small AND cache fast.

### M17. `BackgroundTask(cleanup_workspace, ws)` runs SYNCHRONOUSLY in newer Starlette versions
**Phase ref:** 03 step 8
**Problem:** Starlette's `BackgroundTask` is async-aware but if `cleanup_workspace` is sync (it is — `shutil.rmtree`), it blocks the event loop briefly. For a per-request cleanup that's < 100ms, OK. For a workspace with thousands of files, problem.
**Fix:** Use `BackgroundTasks` (plural) with explicit async wrapper; or run cleanup via `asyncio.to_thread(shutil.rmtree, ws)`.

### M18. Stream chunker in phase 04 splits on whitespace; non-Latin-script content (Chinese/Japanese) has none
**Phase ref:** 04 step 2 `_chunk_text`
**Problem:** `text.split(" ")` for CJK languages yields one giant token → no streaming-feel.
**Fix:** Fall back to character-window if no whitespace found within size×2 chars. Or split by codepoint count.

### M19. `--max-tokens` honored as soft truncate AFTER content already streamed
**Phase ref:** 03 step 7
**Problem:** Phase 03 truncates collected text after the fact, but stream already emitted those bytes to the client. `finish_reason="length"` arrives but client already received the over-quota content.
**Fix:** Acceptable — `max_tokens` is a soft contract on OpenAI side too. Document.

### M20. Lua `ZRANGE+sum` for TPM at 200k tier means up to 12k entries each check
**Phase ref:** 06 §Architecture note + Q2
**Problem:** Q2 unresolved — explicitly. Live load test deferred to phase 10. Risk: TPM check goes from <1ms to 50ms at peak, blowing the "p99 middleware overhead < 5ms" target.
**Fix:** Either implement the parallel-counter alternative now or commit to it as fallback. Don't ship the ZSET-sum approach without load test.

---

## Low / Nitpicks

### L1. Phase 03 ID format `chatcmpl_<26 hex>` doesn't match OpenAI's `chatcmpl-<base32>` (hyphen, not underscore)
**Phase ref:** 03 step 4
**Problem:** OpenAI uses `chatcmpl-` (hyphen). Plan's `chatcmpl_` (underscore) is non-canonical. Cosmetic but compat tests may parse format-strictly.
**Fix:** Use hyphen.

### L2. Bearer header parsing rejects scheme other than "Bearer" — OK, but case-insensitive comparison?
**Phase ref:** 01 step 7 — says "case-insensitive" — confirmed. No issue.

### L3. README explains how to do `codex login` on headless prod server
**Phase ref:** 10 §Runbook entry #1
**Problem:** "Bootstrap host (interactive `codex login`)" — phase says "via SSH X11 forwarding" implicitly but doesn't document. `codex login` opens a browser for OAuth; on headless server, no browser. `--device-auth` flow exists (researcher-01 §5) but the runbook doesn't reference it.
**Fix:** Runbook §1 must specify `codex login --device-auth`, copy device code, complete OAuth on laptop browser.

### L4. Phase 09 coverage CI artifact upload — but no codecov.yml
**Phase ref:** 09 step 11
**Problem:** Artifact uploaded; what surfaces it on the PR? Without Codecov integration or PR comment, it's invisible.
**Fix:** Either add codecov.io upload step or use github-actions/coverage badge.

### L5. Multiple phase files reference `request.state.api_key_id` — phase 01 sets `request.state.api_key`
**Phase ref:** 01 step 8 vs 06 step 7
**Problem:** Inconsistent attr name. `request.state.api_key` (object) vs `request.state.api_key_id` (uuid). Phase 06 expects the latter.
**Fix:** Pick one; phase 01 should expose both for clarity.

### L6. Phase 08 webhook helper "Slack/email POST helper" but only Slack shape shown
**Phase ref:** 08 step 13
**Problem:** Email isn't HTTP. SMTP via httpx? The mention is loose.
**Fix:** Drop email or use a service like Resend/SendGrid HTTP API.

### L7. Plan.md "Acceptance: All 11 phases marked completed" — no acceptance per-phase besides success-criteria
**Problem:** Soft definition of done.
**Fix:** Plan.md should reference phase-level success-criteria as the gate.

### L8. Phase 02 "background poller every 5 min" doesn't add jitter
**Phase ref:** 02 step 6
**Problem:** Multiple instances start polls at exact intervals — thundering herd against `codex auth status`.
**Fix:** Add `random.uniform(-30, 30)` jitter. Phase 08 mentions "jitter to poller interval" as future work — bring forward, trivial.

### L9. Phase 07 sampling "always-on for spans w/ status=ERROR" — sampler decision happens at span START, not end
**Phase ref:** 07 step 6
**Problem:** OpenTelemetry sampler decides at span-creation; status set at span-end. ParentBased+TraceIdRatio doesn't observe outcome. The "always-on for ERROR" is a tail-based-sampling concept needing a separate processor (TailSamplingProcessor in collector, not gateway).
**Fix:** Move tail sampling to otel-collector config, not gateway sampler.

### L10. Phase 05 `audit_log.append_audit` referenced but never defined in phase 01 or 05
**Phase ref:** 05 step 12
**Problem:** Cross-reference dangling. Phase 08 defines `audit_log.emit`. Match names.

### L11. `ADMIN_TOKEN` env loaded as `SecretStr` but compared with `secrets.compare_digest` — `SecretStr` repr is `***`; `compare_digest` accepts str or bytes
**Phase ref:** 01 step 9 vs 08 step 8
**Problem:** Phase 01 uses `secrets.compare_digest`; phase 08 uses `hmac.compare_digest`. Both fine. But `SecretStr.get_secret_value()` returns str — passes. Trivia.

---

## YAGNI Hits — features to cut

| Item | Phase | Justification |
|------|-------|---------------|
| Reasoning items emission for Responses API | 04 (deferred to 08) | Codex on ChatGPT free/paid plan rarely emits reasoning items in non-o-series flows. Phase 08 already drops it. **Remove the deferred-to-phase-08 reference.** |
| 3 Grafana dashboards | 07 | Ship 1 (API Overview). The others are "before-10-users" work. Add as needed. |
| age-encrypted Postgres backup | 10 | Single-VM v1 with no compliance requirement → standard pg_dump to encrypted S3 bucket suffices (S3-managed encryption). Add age when compliance demands it. |
| OpenTelemetry full setup (3 instrumentors + collector) | 07 | structlog logs ingested by Loki + Prom metrics is enough for v1. Tracing adds compose+config overhead; skip until first incident demands it. (Counter-argument: tracing across queue is the one thing you'll wish you had during outage. Keep but mark trade-off.) |
| Node SDK compat tests | 09 | Python SDK is the canonical; Node parses the same wire format. **Defer Node tests to v1.1.** Saves a Docker image + test runner complexity for ~10% added confidence. |
| `infra/grafana/*.json` committed | 07 | Manual export is a maintenance burden — JSONs drift from live dashboards. Use Grafana provisioning from a templated YAML, or commit dashboard URLs only. |
| Webhook alert helper supporting Slack vs HTTP | 08 | Pick one. Slack covers 90% of v1 ops needs. |
| `replaces_id` rotation with 24h grace | 08 | Adds a column + audit complexity. Simpler v1: revoke old, force client re-config (downtime by design). 24h grace is convenience that hides botched rotations. |
| `run_tests` reserved field rejection | 05 | Adding rejection for a field that doesn't exist yet is overengineering. Remove from request schema; clients hitting a 422 on unknown field is the standard pattern (FastAPI extra=forbid). |
| Tier-tunable timeout per-route | 08 | Phase 08 §timeouts table marks "Tier-tunable: yes" for chat/responses. v1 doesn't need per-tier timeouts; one default. Add when a paying customer demands it. |
| `--ephemeral` AND `--skip-git-repo-check` AND `--ask-for-approval never` AND model AND search | 02 step 5 | Keep `--ask-for-approval never` and `--cd`. Trim others to bare minimum until proven needed. |

---

## Missing entirely

### MM1. SSE keepalive/heartbeat for long Codex runs
None of the plan's SSE paths emit periodic keepalive comments. A 5-minute Codex run with no agent_message events for 90s will hit Caddy/AWS-ALB idle timeouts (typical default 60s) — **stream silently dies mid-run**. Phase 10 sets Caddy to 1h, but mobile networks, NAT timeouts, and Cloudflare (if added) impose stricter idle limits. OpenAI itself emits `: <comment>` keepalive every ~10s.
**Fix:** Add to phase 03 + 04 stream handlers: spawn parallel task that yields `: keepalive\n\n` every 15s while Codex is silent.

### MM2. Database migration strategy / rollback procedure
No phase covers "what if migration N fails halfway in production". Alembic supports `downgrade` but there's no documented procedure, no test that downgrade actually works for any migration past trivial.
**Fix:** Phase 10 runbook entry. Each migration phase should include downgrade roundtrip test.

### MM3. Idempotency-Key support for POST endpoints
OpenAI's API supports `Idempotency-Key` header. SDK retries inject one. Without server-side dedup, a network timeout retry creates a second job. Phase 05 jobs, in particular, should dedupe.
**Fix:** Phase 05 schema: optional `Idempotency-Key` header → stored in jobs row UNIQUE(user_id, idempotency_key). Replay returns existing job.

### MM4. Request body size limits beyond prompt char count
Phase 08 has `PROMPT_MAX_CHARS=262144`. But pydantic JSON parsing happens BEFORE the validator. A 10MB JSON body with 200k chars + huge metadata still parses then validates. FastAPI / starlette doesn't limit body size by default.
**Fix:** Add to gateway middleware: reject bodies > 1MB at the ASGI layer before pydantic touches them.

### MM5. PII handling and GDPR-style data deletion
Audit_log stores prompt_hash + user_id + tokens. If user is in EU, GDPR delete request requires erasure. No phase mentions this.
**Fix:** Phase 08 should include a `DELETE /admin/users/{id}/data` endpoint that purges audit rows + revokes keys + sets user.deleted_at.

### MM6. Codex stderr persistence for postmortems
Phase 02 caps stderr at 64 KiB ring buffer in-process. Phase 08 audit_log truncates. After a crash, stderr is lost. On-call cannot diagnose "codex crashed at 3am, what was the stderr message?"
**Fix:** Phase 08 should persist last 4 KiB of stderr to `jobs.stderr_tail` column on terminal status (already partially planned via `EXIT_NONZERO` synth event), AND ship full stderr to log shipper as a tagged event.

### MM7. Health check distinguishability — `/healthz` vs `/readyz` vs `/livez`
Phases use only `/healthz` and `/readyz`. Kubernetes-style monitoring expects `/livez` (am I alive, never mind dependencies) for restart decisions. Single-VM compose doesn't strictly need it, but Caddy upstream healthcheck should hit `/livez`, not `/readyz` (else Caddy stops routing on Codex session expiry — DEFAULT-DENY all traffic when actually we want degraded service).

**Disagreement intentional:** the plan says `/readyz` SHOULD return 503 on session-expiry to "shed load gracefully". This is wrong for Caddy upstream check — it removes the ONLY upstream and serves 502 to clients. Better: `/livez` is for Caddy (always 200 if process alive), `/readyz` is for orchestrator (Kubernetes future), and codex-session-expiry uses a SEPARATE `/v1/codex/health` that the API can check before accepting requests.

### MM8. Test for "what happens at 100 RPS sustained for 10 minutes"
Phase 09 success metric "Test stack readiness < 30s; full suite < 5 min in CI" — but no LOAD test. Phase 06 says "± 1% rate-limit accuracy at 100 req/s" but the test in phase 06 only fires 21 sequential requests.
**Fix:** Phase 09 or 10 add a k6/locust load test profile, run pre-deploy.

### MM9. ChatGPT account pool migration — no data structures planned
brainstorm §11 "Multi-account pool — defer v1.1". v1.1 will need a pool, rotation logic, health-aware routing. Schema doesn't reserve `accounts` table or `account_id` FK on jobs. v1.1 is a schema migration nightmare unless reserved now.
**Fix:** Add `account_id` (nullable) FK to jobs/audit_log NOW. Saves a painful migration later.

### MM10. Disk-fill DoS via prompt-controlled `git clone`
A user can submit a job for a 4GB monorepo. Free tier cap = 100k monthly tokens (~300 chat completions) but jobs are not token-bounded. Tier limits don't cap clone size. With concurrent=2 you can have 2 simultaneous huge clones × 5min each = 4-8GB tmpfs sustained → workspace OOM (cf H7).
**Fix:** Pre-clone size check: GitHub API content-length probe. Reject if repo size > tier limit (free: 50MB, pro: 500MB, ent: 5GB).

### MM11. No mention of Postgres connection pooling / pgbouncer
At 100 RPS multi-worker, direct Postgres connections from each uvicorn worker × pool_size = 80+ idle connections. pgBouncer transaction-mode pooler is standard practice. Phase 10 doesn't mention.
**Fix:** Add pgBouncer container in phase 10 OR document why deferred.

### MM12. Argon2 timing oracle on prefix-hit vs prefix-miss (reformulation of M4)
Already in M4. Tracking only.

### MM13. No coverage of CHATGPT session refresh requiring browser interaction
brainstorm risk row: "Session token expiry — healthcheck cron, alert + auto-disable". When alert fires at 3am — what is on-call expected to do? Re-run `codex login` requires browser. Runbook §9 says "recover from session expiry" but the procedure isn't shown.
**Fix:** Runbook §9 must include exact commands: `ssh prod-host`, `docker compose exec gateway bash -c 'codex login --device-auth'`, copy device code, complete OAuth on phone, verify `codex auth status`. **AND** must say what users see during the 5-15 minutes downtime (503 from /readyz). Ideally: caching last successful response or returning 503 with `Retry-After: 600`.

---

## Unresolved questions for product/lead

1. **Legal/ToS posture on ChatGPT account use** — has anyone confirmed with legal counsel that operating a paid SaaS off a single ChatGPT subscription is permissible? If not, **DO NOT SHIP TO PAYING CUSTOMERS** under v1's single-account locked decision. Engineering has done its job; this is a product/legal gate.

2. **Real Codex 0.125.0 flag verification** — has anyone run `codex exec --help` against the actual binary and saved the output? If not, do this BEFORE phase 02 begins. Specifically confirm: `--ephemeral`, `--skip-git-repo-check`, `--ask-for-approval never`, `--color never`, and that JSONL is on stdout under `--json` (not Unix socket per the 0.125.0 changelog hint).

3. **Audit log durability requirement** — is "best-effort, fire-and-forget, occasional drop OK" acceptable for compliance/billing? If yes, current design fine after C8 fix. If no, audit must be synchronous → request latency hit + audit DB outage = API outage. Pick one.

4. **Single-account uptime SLA** — phase 10 success criteria includes 99.5% uptime / 30d. ChatGPT session refresh + occasional account-level rate limits will exceed 0.5%/month. Drop SLA to 99% or carve out "scheduled session-refresh windows" in writing.

5. **Body size + prompt size — choose one canonical limit** — phase 08 has `PROMPT_MAX_CHARS=262144`; phase 03 has `CHAT_MAX_PROMPT_CHARS=200000`. These conflict.

6. **TPM true-up algorithmic decision (C5)** — confirm with team: drop true-up entirely (overcharge based on est) vs implement parallel-counter alternative. Either is fine; the negative-ZSET approach is broken.

7. **Job repo size limit per tier** — what's the policy? Without one, MM10 is a viable DoS.

8. **`/admin/api-keys` in v1 — is "admin endpoint with single shared admin token" OK long-term?** No multi-admin, no IAM. If team has > 1 ops person, this is a credential-sharing antipattern. v1 acceptable; document as v1.1 work.

9. **Per-VM scaling** — phase 10 single VM. At what user/RPS count does multi-VM become urgent? Should phase 10 already include readiness for horizontal split (stateless gateway + shared Postgres + shared Redis)? Compose on single VM doesn't preclude horizontal but Caddy ACME state is per-VM.

10. **Codex `--search` flag** — phase 02 step 5 includes optional `--search`. Researcher-01 §4 mentions `--search` enables web search. Public exposure of web-search via our API has its own legal/cost implications (the wrapper user makes web searches against ChatGPT plan's web tool — counts against account limits). Disable for v1?

---

## Positive observations

(Lead with what's broken, but credit where due.)

- **Locked decisions doc + risk table format is excellent.** Nothing handwaved; each row has rationale.
- **Pydantic discriminator unions for Codex events** (phase 02 step 2) is the right approach for forward-compat.
- **`extra="allow"` on event base + `extra="ignore"` on chat request** strike correct balance: tolerate unknown from upstream, silently drop unknown from clients (vs forbid which would break SDK retries with new fields).
- **Path-traversal symlink check** (phase 02 step 4) is application-layer defense-in-depth on top of sandbox — correct posture.
- **`{`-prefix guard on stdout JSONL** is a clean fix for issue #15451 (researcher-01).
- **`code: invalid_request_error` envelope shape** matches OpenAI exactly (researcher-02 §A.4 / §B.3.13). Compat will hold.
- **`finally: cleanup_workspace`** consistently used in phase 03/05 — not an afterthought.
- **`update_last_used_fire_and_forget` correctly identified as hot path** (phase 01); just needs the C8 fix.
- **Constant-time admin token compare** mentioned in BOTH phase 01 and phase 08 — consistent guidance.
- **Mock-codex fixture approach** (phase 09) is the right call for CI; real-codex weekly cron suggested in risk table is the right escape valve.
- **Phase 06 acknowledges Redis-down → fail-OPEN** with explicit trade-off — security-conscious teams often flip this; documenting is good.
- **Researcher reports are concrete and grounded** — researcher-01 catalogs unresolved questions (esp. cancel semantics) honestly. Plan inherits the honesty.

---

## Recommended Actions (priority-ordered)

1. **Halt phase 02 implementation** until Codex 0.125.0 flags + JSONL output verified against real binary (C1, C11). 30 minutes of work, prevents weeks of rewrites.
2. **Resolve ChatGPT-login legal posture** (C7). Product/legal blocker.
3. **Fix middleware pre-auth IP rate limit** (C2). 1 day. Prevents day-1 DoS.
4. **Replace BaseHTTPMiddleware in SSE path with raw ASGI middleware** (C3). 2-3 days. Without this, streaming is broken in prod.
5. **Drop or correct TPM true-up Lua approach** (C5). Either remove true-up (overcharge based on est) OR implement parallel-counter. 1-2 days.
6. **Fix audit_log fire-and-forget pattern** (C8) + bump pool size (C9). 1 day.
7. **Add SSE keepalive** (MM1). Half day. Prevents idle-timeout silent stream death.
8. **Bring argon2 cache forward to phase 01** (H8). Half day in phase 01 vs days of rework in phase 06.
9. **Fix `validate_path_inside` symlink check** (C6) using realpath approach. 2 hours.
10. **Cut YAGNI items** — Node SDK tests (defer to v1.1), 3-board Grafana → 1, age encryption → S3 native, deferred-to-phase-08 reasoning → drop. Save ~5 days.
11. **Add Idempotency-Key for /jobs** (MM3). 1 day, saves user complaints.
12. **Document runbook §9 (session-expiry recovery) end-to-end NOW**, not at phase 10. Half day.

---

## Metrics

- **Critical issues:** 11
- **High issues:** 15
- **Medium issues:** 20
- **Low / nitpicks:** 11
- **YAGNI items to cut:** 11
- **Missing entirely:** 13
- **Unresolved Qs:** 10
- **Total flagged:** 81 issues across 11 phases (~7.4 issues/phase, normal density for production-grade plans of this scope)
- **Phases with critical issues:** 02, 03, 04, 05, 06, 08 (the implementation-heavy ones)

---

**Status:** DONE_WITH_CONCERNS
**Summary:** 11 critical issues, 15 high. Plan is structurally sound but contains production-breaking flaws (Codex flag verification gap, BaseHTTPMiddleware/SSE incompatibility, broken TPM true-up math, fire-and-forget audit pattern leaking pool, missing keepalive, missing IP-coarse limiter, ChatGPT ToS exposure). Recommend fixing C1-C11 + MM1 before phase 02 implementation begins. Estimated impact: ~2 weeks of rework prevented; ~4 weeks saved by acting on YAGNI cuts.
**Concerns/Blockers:** ChatGPT ToS legal posture (C7) is a product/legal gate, not engineering — escalate to lead before any user-facing launch.
