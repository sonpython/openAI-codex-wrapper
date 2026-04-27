# Phase 05 Code Review — `/v1/codex/jobs` + Arq worker

**Scope:** routes/jobs.py, schemas/jobs.py, db/models.py (Job), migration 0003, db/crud/jobs.py, workers/* (event_publisher, arq_worker, job_handlers, git_clone, git_diff), gateway/app.py wiring, settings.py, 6 test files.

## Verdict

**APPROVE_WITH_CHANGES** — design solid, spec adherence high, but **one critical production-only bug** (caught by tests-with-mocks, fatal at runtime) plus several high-priority correctness issues.

---

## Critical (BLOCKING)

### C1. `async with get_session()` is broken — fails at runtime on every request
`src/gateway/routes/jobs.py:69, 121, 146, 200`. `get_session` in `db/engine.py:96` is an **async generator** (`yield session`) intended only as a FastAPI `Depends(...)` dependency. It is NOT an async context manager. `get_session()` returns an `async_generator` object; `async with` on it raises `TypeError: 'async_generator' object does not support the asynchronous context manager protocol`.

Tests pass only because they `patch("src.gateway.routes.jobs.get_session", return_value=mock_session)` and inject a mock CM. Auth middleware (`middleware/auth.py:147`) and other modules use `main_session()` (the factory, returns a true async CM) — that is the right pattern for non-FastAPI-Depends contexts.

Production impact: 100% of POST/GET/DELETE/events return 500 with `TypeError`. Replace `get_session()` with `main_session()` (or convert to FastAPI dependency injection).

### C2. SSE `_replay_then_subscribe` leaks the redis pubsub on every replay-only path
`routes/jobs.py:209, 247-273`. When the backlog already contains a terminal event (the common case for a completed job re-fetched from history), the generator returns at line 244 BEFORE `pubsub = redis.pubsub()` is created — so this case is fine. But once we enter the live loop, an exception inside the loop (e.g. `request.is_disconnected()` raising, JSON malformed, etc.) is **caught only by `finally`**, which calls `pubsub.unsubscribe + aclose`. The bare `except Exception: pass` (line 272) silently swallows all errors and may leave a half-closed connection in the pool. Worse: on Redis client disconnect mid-`listen()`, `pubsub.listen()` async iterator may never unblock (no client-side timeout), so `keepalive_wrap` shielding masks the deadlock — connection held forever. Add explicit `pubsub.listen()` timeout via `async with asyncio.timeout(...)` per iteration, OR poll `get_message(timeout=...)`.

---

## High

### H1. POST inserts DB row, then enqueues — orphan job on Arq failure
`routes/jobs.py:69-84`. Insert happens, `session.commit()` runs at line 78; THEN `arq_pool.enqueue_job` at line 84. If enqueue raises (Redis down, network, OOM), the job row is committed `status=queued` but no Arq task exists → row stuck queued forever, no worker will ever pick it up. Spec acknowledged this risk implicitly. Two options: (a) reverse order — enqueue first, insert after, with cleanup-on-DB-failure; (b) keep current order but wrap enqueue in try/except and on failure mark the row failed with `error_code=enqueue_failed`. Option (b) is simpler and idempotent.

### H2. DELETE on `queued` job double-publishes `job.cancelled`
`routes/jobs.py:152-161` marks DB cancelled and publishes `job.cancelled` immediately. The Arq job has NOT been aborted (spec called for `arq_pool.abort_job(...)`, never implemented). When the worker later dequeues the same job (`job_handlers.py:66`), the cancel flag is still set so it publishes a SECOND `job.cancelled` event. SSE clients see two terminal events. Either: (i) call `arq_pool.abort_job(job_id)` to dequeue, or (ii) gate the worker's early-cancel publish on "did the API already publish"? Simpler: just have API set the flag + DB-update-only (no publish); let worker's path emit the lone terminal event. Or accept two terminals as benign (replay loop returns on first).

### H3. Mid-run cancel: worker's `bg_session` write races with API DB write
`routes/jobs.py:155` (queued path) marks DB `cancelled` with `finished_at=now()`. Meanwhile, if worker already moved the job to `running` (race window: API sees `status=queued`, but worker just wrote `running`), the API silently overwrites running → cancelled. Worker continues, sees its own cancel flag, marks `cancelled` again. End state correct, but workspace_path is set to `None` after API write → audit ambiguity. Mitigation: API should only `mark_cancelled` if `status='queued'` is asserted in the WHERE clause of the UPDATE (compare-and-set). `crud.mark_cancelled` does no status guard (`db/crud/jobs.py:138-142`).

### H4. `repo_url` regex permits trailing slash but not nested user/repo formatting
`schemas/jobs.py:23` and `workers/git_clone.py:23` (good — re-validated). Verify covers the cases:
- ✅ rejects SSH (`git@github.com:...`), HTTP, GitLab, localhost, 127.0.0.1, embedded creds (test_jobs_request_schema covers).
- ⚠️ accepts `https://github.com/a/b.git/` — note the trailing-slash + `.git` double form. Not security-critical (git clone tolerates it), but inconsistent with canonical form.
- ⚠️ does NOT reject `https://github.com/a/b/` followed by extra ASCII (looks rejected because regex is anchored, but check `https://github.com/a/.b` — leading dot in repo segment is allowed by char class. Possibly creates a hidden dir? Low risk).
- ⚠️ Branch validator at `schemas/jobs.py:24` permits leading `-` (e.g. branch=`-upload-pack`). `git clone -b -<flag>` doesn't help an attacker because branch is positional after `-b`, but defense-in-depth: reject leading `-` and `..` segments. Particularly `..` could be embedded in `-b` to traverse if git ever interprets it (low risk on `--depth 1` clone, but worth a test).

### H5. `git_diff.capture_diff` doesn't bound stdout — OOM risk on giant diffs
`workers/git_diff.py:36-46`. `proc.communicate()` reads the entire stdout into memory before truncating. Spec §risk-table called for "stream `git diff` to file, then read with size cap". A malicious or huge diff could OOM-kill the worker before the 16 MB cap is applied. Mitigation: spawn with `stdout=PIPE`, read in chunks via `proc.stdout.read(chunk_size)`, accumulate up to cap, then kill subprocess.

### H6. `mark_succeeded` lookups `summary_parts` from `evt.item.text` — wrong attribute path on some events
`workers/job_handlers.py:142-147`. `hasattr(evt.item, "type") and evt.item.type == "agent_message"` — the attribute path. For Codex ItemCompleted with non-agent-message items (e.g. `tool_call`), `evt.item` exists but `evt.item.text` may not. The code guards with `hasattr(evt.item, "text")`, OK. But `evt.item.text` is concatenated using `\n`.join on a list — multi-turn agent_messages are joined without delimiter clarity. If summary is empty but stderr has content, we lose useful diagnostics. Consider falling back to last 200 chars of stderr_tail when summary is empty.

---

## Medium

### M1. SSE auth: skip-list does not include `/v1/codex/jobs/*/events`, so AuthMiddleware enforces auth. Good. But middleware authenticates via Bearer header — browsers using EventSource cannot send custom headers. SDK clients using fetch+ReadableStream are fine. Document this constraint.

### M2. `JobResponse.from_job` truncates by encoding ENTIRE diff_blob to bytes (`schemas/jobs.py:115`). For a 16 MB blob this allocates 32 MB+ momentarily. Use `len(raw_diff)` chars (≤ 1 MB chars ≈ ≤ 4 MB bytes) as fast-path threshold; only encode if char count suggests it might exceed cap.

### M3. `arq_worker.WorkerSettings` calls `_build()` at import time (`arq_worker.py:82`). This reads env vars BEFORE Arq sets up its own logging, fine — but it caches `redis_settings`. If env changes between import and worker start (e.g. test fixtures), worker connects to wrong Redis. Low risk in practice.

### M4. `event_publisher.publish_job_event` uses `pipeline(transaction=False)` — RPUSH and PUBLISH are NOT atomic. A live SSE subscriber may receive PUBLISH before the LIST is updated; if it then disconnects + reconnects, LRANGE may miss the event. Spec §risk acknowledged this. Replay-then-subscribe pattern partially mitigates, but ordering is theoretically violable. Use `transaction=True` (MULTI/EXEC) — minor latency cost, large semantic gain.

### M5. Worker `mark_failed` on `clone_failed` does NOT cleanup workspace before the `finally` block runs — actually it does (workspace assigned at line 88, `finally` at line 220 always runs). OK. But when clone fails, `workspace_path` is set in DB but the dir is gone post-cleanup → audit shows a dangling path. Minor.

### M6. `recover_orphan_jobs` does NOT clean up orphan workspace dirs on disk (`job_handlers.py:225-246`). On worker restart we mark the row failed, but `/workspaces/{job_id}` from the dead job stays on disk. Disk fills. Spec §next-steps mentions cron janitor in phase 08 — acceptable for v1, but add a TODO comment.

### M7. `JobResponse` includes `task` field (the user's prompt) verbatim in API response. If the prompt contains secrets the user accidentally pasted, GET /jobs/{id} echoes them back. By design (the user owns the data), but worth noting for redaction-by-default in phase 07.

---

## Low / Nitpicks

- L1. `routes/jobs.py:47` imports `_arq_pool` from `app.py` lazily inside function — works but the `noqa: PLC0415` is a smell. Better to attach the pool to `app.state.arq_pool` and read via `request.app.state`.
- L2. `routes/jobs.py:84` enqueue does not pass `_queue_name` and worker does not set `queue_name` — both default. Spec called for `ARQ_QUEUE_NAME=codex_jobs`. Either add the setting or remove the spec line.
- L3. `routes/jobs.py:168` re-fetches job after DB write to return fresh state — OK, but uses the same session that just committed. Should be fine with autoflush, but consider `await session.refresh(job)` instead.
- L4. `event_publisher` imports inside route function (`routes/jobs.py:93, 157`) — module-level import is cleaner.
- L5. `git_clone.py:78` raises `GitCloneError` on timeout, but the `from None` suppresses cause — keep `from exc` for traceability.
- L6. Migration 0003 does not add a CHECK constraint on `status` IN ('queued','running','succeeded','failed','cancelled') — invalid status writes are caught in code, not DB. Defense-in-depth nice-to-have.
- L7. `git_diff.py:46` discards stderr — on `git diff` failure (rare), no diagnostic. Log it.
- L8. `JobResponse` does not surface `cancelled_at` (model also lacks it; the spec mentioned it). `finished_at` is reused for all terminals — adequate but less informative.

---

## Spec Adherence

| Spec item | Status |
|---|---|
| Public-only GitHub URL regex | ✅ |
| `run_tests=True` rejected | ✅ |
| 202 + UUID returned | ✅ |
| Branch validation | ⚠️ leading `-` allowed |
| Cancel flag mechanism | ✅ |
| Replay-then-subscribe SSE | ✅ |
| Workspace cleanup on every exit | ✅ (try/finally) |
| Orphan recovery on worker boot | ✅ |
| 16 MB diff cap | ✅ |
| 4 KB stderr cap | ✅ |
| Arq `abort_job` on queued cancel | ❌ not implemented |
| `_queue_name` setting | ❌ deviation |
| `arq.RedisSettings.from_dsn` | ✅ |
| Authentic owner-scoped 404 | ✅ |
| Async timeout wraps codex stream | ✅ (line 125) |
| Workspace path under WORKSPACE_ROOT | ✅ (delegated to make_workspace) |

---

## Strengths

- Well-organised modules, all under 200 LOC except where data classes naturally cluster.
- Clean separation: `git_clone` / `git_diff` / `event_publisher` / `job_handlers` each single-purpose.
- Tests cover all primary paths with appropriate mocking.
- Pipeline batching for Redis writes is the right call.
- `try/finally` cleanup contract is honoured even on unexpected exceptions.
- URL re-validation at exec time (defense-in-depth) is a nice touch.
- Sandbox mode (`read-only` vs `workspace-write`) correctly mapped to runner flag (`allow_write` boolean).

---

## Unresolved Questions

1. Should the `queued`-DELETE path call `arq_pool.abort_job(job_id)` to dequeue, or is the worker's early-cancel branch sufficient given the duplicate-event side effect? (relates to H2)
2. `transaction=True` on the event-publisher pipeline — acceptable latency cost? (M4)
3. Do we want a CHECK constraint on `status` enum at DB level? (L6)
4. EventSource browser clients can't auth via Bearer header — do we need an auth-via-cookie or query-string token for that path, or document SDK-only support? (M1)

---

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical count:** 2 (C1 must block merge; C2 strongly recommended before merge)
