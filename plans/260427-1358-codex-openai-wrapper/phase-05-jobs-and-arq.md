# Phase 05: Jobs API + Arq Worker

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§5 schema, §11 cancellation in v1, public-only clone)
- Codex runner: phase-02-codex-runner.md (subprocess + JSONL parser + workspace mgmt)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (§8 timeout/cancel — codex doesn't emit final event on SIGTERM in non-interactive mode → wrapper must handle)
- OpenAI taxonomy: research/researcher-02-openai-event-taxonomy.md (Part B SSE format reused for `/jobs/{id}/events`)
- Phase 00: phase-00-bootstrap.md (settings: `WORKSPACE_ROOT`, `JOB_TIMEOUT_SECONDS`, `JOB_CANCEL_GRACE_SECONDS`)
- Phase 01: phase-01-auth-and-models.md (bearer auth → user_id)

## Overview
- Priority: high
- Status: pending
- Effort: L
- Description: Long-running Codex task endpoint. `POST /v1/codex/jobs` enqueues a job onto Arq (Redis-backed) for asynchronous execution: clone public GitHub repo → run `codex exec --json` against it → capture diff + summary + files_changed. `GET /v1/codex/jobs/{id}` returns full job state. `GET /v1/codex/jobs/{id}/events` streams lifecycle events via SSE (live during execution, replay from db post-completion). `DELETE /v1/codex/jobs/{id}` cancels: dequeue if queued, SIGTERM→SIGKILL if running.

## Key Insights
- **Public-only repos v1** (brainstorm §11): regex-validate URL `^https://github\.com/[\w.-]+/[\w.-]+(\.git)?$`; reject 422 with actionable error. Private/SSH/PAT support deferred.
- **Codex non-interactive cancel** (researcher-01 §8): no guaranteed final event on SIGTERM. Wrapper MUST detect cancel via flag, not by waiting for codex error event. Set `cancelled_at` in db, mark final state directly, emit synthetic `job.cancelled` SSE event.
- **Diff capture via plain git** (brainstorm §6): after codex exits, run `git -C {repo} diff` and `git -C {repo} diff --name-only` against the original HEAD recorded pre-codex. `git status --porcelain` for untracked files.
- **Replayable SSE** (event log): every emitted job event ALSO appended to a Redis list `job:events:list:{job_id}` so late subscribers replay from start. After job terminal state, list TTL = 24h, then audit_log row holds permanent record (truncated).
- **Cancellation poll** (single source of truth): worker polls Redis key `cancel:job:{job_id}` every 1s in async loop. Setting key from API handler is the only cancel mechanism — no direct signal across processes. Worker on detect → `process.terminate()` → wait `JOB_CANCEL_GRACE_SECONDS` → `process.kill()`.
- **Diff blob size cap**: store full blob in `jobs.diff_blob` text column up to 16 MB; GET endpoint returns diff truncated to 1 MB with `diff_truncated: true` flag and a `diff_url` for full-fetch (deferred until phase 08 — for v1 the GET response carries the truncated text + length).
- **`run_tests` reserved for v1.1**: schema accepts the field but rejects `true` with explicit error so future clients can probe support.

## Requirements

### Functional
- `POST /v1/codex/jobs` — accepts `{repo_url, branch, task, mode, run_tests?, timeout_seconds?}`; validates; creates `jobs` row status=`queued`; enqueues Arq job; returns 202 + `{id, status, created_at}`.
- `GET /v1/codex/jobs/{id}` — returns full job state. 404 if not found, 403 if owner mismatch.
- `GET /v1/codex/jobs/{id}/events` — SSE stream of lifecycle events; replays from start via Redis list, then subscribes to live pub/sub channel; on terminal state, sends final event + closes.
- `DELETE /v1/codex/jobs/{id}` — idempotent cancel; returns 200 with current state. Behavior:
  - status=`queued`: remove from Arq queue, set status=`cancelled`, emit `job.cancelled` event
  - status=`running`: set Redis `cancel:job:{id}` flag; worker handles SIGTERM/SIGKILL; status transitions in worker
  - status terminal: no-op, return current state
- Worker:
  1. Mark `running`, set `started_at=now()`, emit `job.started`
  2. Create workspace `{WORKSPACE_ROOT}/{job_id}`
  3. `git clone --depth 1 -b {branch} {repo_url} {workspace}/repo` with 60s timeout
  4. Record `git rev-parse HEAD` as `head_before`
  5. Stream codex events; for each event: append to Redis list + publish to channel + persist to `audit_log`
  6. On codex exit: `git diff head_before HEAD -- .` capture; `git diff --name-only head_before HEAD`; extract last `agent_message.text` as summary
  7. Mark terminal state (`succeeded`/`failed`/`cancelled`); cleanup workspace; emit terminal event
- Per-job timeout: configurable via `timeout_seconds` request field, capped at tier limit (phase 06); default `JOB_TIMEOUT_SECONDS` from settings.

### Non-Functional
- Each module ≤ 200 LOC.
- Workspace dir is per-job UUID; never reused; cleanup is unconditional (try/finally).
- Worker resilient to crash: orphan workspaces cleaned by lifespan startup scan (any `jobs` row in `running` state at worker boot → mark `failed` with reason `worker_restarted`).
- Job table indexed on `(user_id, created_at DESC)` for list endpoint (list endpoint deferred but index added now).

## Architecture

```
client ─POST /v1/codex/jobs─► routes/jobs.py
                                   │
                                   ├─ validate (public github URL, mode in {read-only, workspace-write})
                                   ├─ insert jobs row (status=queued)
                                   ├─ arq.enqueue_job("run_codex_job", job_id, …)
                                   └─ 202 {id, status: queued, created_at}

client ─DELETE /v1/codex/jobs/{id}► routes/jobs.py
                                   │
                                   ├─ if queued → arq.remove_from_queue + db update
                                   └─ if running → SET cancel:job:{id} = "1" (TTL 5min)

client ─GET /v1/codex/jobs/{id}/events─► routes/jobs.py
                                   │
                                   ├─ replay LRANGE job:events:list:{id} 0 -1
                                   ├─ if not terminal: SUBSCRIBE job:events:{id}
                                   └─ stream until terminal, then close

   ┌─────────────────────────── Arq worker process ───────────────────────────┐
   │ workers/arq_worker.py — WorkerSettings(functions=[run_codex_job], …)     │
   │                                                                          │
   │ async def run_codex_job(ctx, job_id, repo_url, branch, task, mode,       │
   │                         timeout_seconds):                                 │
   │    db.mark_running(job_id)                                                │
   │    publish("job.started", job_id)                                         │
   │    workspace = workspaces.create(job_id)                                  │
   │    try:                                                                   │
   │       git_clone.clone(repo_url, branch, workspace/repo, timeout=60)      │
   │       head_before = git rev-parse HEAD                                    │
   │       async for evt in runner.stream(task, cwd=workspace/repo, mode):     │
   │          if cancel_flag_set(job_id):                                      │
   │             runner.terminate(); raise Cancelled                          │
   │          publish("job.codex_event", job_id, evt)                          │
   │       diff, files = git_diff(workspace/repo, head_before)                │
   │       summary = last_agent_message_text                                   │
   │       db.mark_succeeded(job_id, diff, files, summary, exit_code)         │
   │       publish("job.diff_ready", …); publish("job.completed", …)          │
   │    except Cancelled:                                                      │
   │       db.mark_cancelled(job_id); publish("job.cancelled", …)              │
   │    except Exception as e:                                                 │
   │       db.mark_failed(job_id, str(e)); publish("job.failed", …)            │
   │    finally:                                                               │
   │       workspaces.cleanup(workspace)                                      │
   └──────────────────────────────────────────────────────────────────────────┘

publish(event, job_id, payload):
   redis.RPUSH job:events:list:{job_id} <json>     # replay buffer
   redis.PUBLISH job:events:{job_id} <json>        # live subscribers
   db.audit_log.insert(…)                           # permanent
```

### Cancellation race (timing diagram)

```
T0  client DELETE  ──► api SET cancel:job:{id}=1 (TTL 5m); return 200 with current state
T0+ε worker loop iteration → reads cancel flag = "1"
T0+ε+δ worker calls process.terminate() (SIGTERM)
T0+5s   worker sees process still alive → process.kill() (SIGKILL)
T0+5s+ε worker raises Cancelled → finally block cleanup
T0+5s+δ db.mark_cancelled + publish job.cancelled

If cancel flag set BEFORE worker starts execution (race with arq dequeue):
  - check flag at top of run_codex_job; if set → mark_cancelled immediately, no clone
```

## Related Code Files

### To create
- `src/gateway/routes/jobs.py` (≤ 200 LOC) — POST/GET/DELETE/events endpoints
- `src/gateway/schemas/openai_jobs.py` (≤ 150 LOC) — pydantic request/response shapes
- `src/workers/arq_worker.py` (≤ 100 LOC) — `WorkerSettings` entry, redis pool config, lifecycle hooks
- `src/workers/job_handlers.py` (≤ 200 LOC) — `run_codex_job` async handler
- `src/workers/git_clone.py` (≤ 120 LOC) — async subprocess git clone with timeout, stderr capture, URL re-validation
- `src/workers/git_diff.py` (≤ 100 LOC) — `git diff` + `git diff --name-only` capture
- `src/workers/event_publisher.py` (≤ 100 LOC) — Redis RPUSH + PUBLISH + audit_log insert in one call
- `src/db/crud/jobs.py` (≤ 200 LOC) — insert, mark_running, mark_succeeded/failed/cancelled, get_by_id, list_orphans
- `src/db/migrations/versions/00X_jobs_table.py` — alembic revision adding `jobs` table per brainstorm §5
- `tests/unit/test_git_clone_url_validation.py`
- `tests/unit/test_jobs_schema.py`
- `tests/integration/test_jobs_lifecycle.py` — full enqueue → run → succeed roundtrip with fake codex
- `tests/integration/test_jobs_cancel.py` — running cancel via DELETE → assert SIGTERM path

### To modify
- `src/db/models.py` — add `Job` SQLAlchemy model
- `src/gateway/app.py` — register `jobs_router`
- `src/settings.py` — add `ARQ_MAX_JOBS=4`, `ARQ_QUEUE_NAME=codex_jobs`, `GIT_CLONE_TIMEOUT=60`
- `Dockerfile.worker` — confirm `git` installed (already in phase 00)

### To delete
- (none)

## Implementation Steps

1. **Migration `00X_jobs_table.py`**: create `jobs` table per brainstorm §5 schema. Index on `(user_id, created_at DESC)`. Columns include `cancelled_at`, `head_before` (for diff anchor), `diff_size_bytes` (for cap enforcement).

2. **Schema** (`schemas/openai_jobs.py`):
   ```python
   class CreateJobRequest(BaseModel):
       repo_url: str
       branch: str = "main"
       task: str
       mode: Literal["read-only", "workspace-write"] = "read-only"
       run_tests: bool = False
       timeout_seconds: int | None = Field(None, gt=0, le=3600)

       @field_validator("repo_url")
       def public_github(cls, v):
           if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+(\.git)?$", v):
               raise ValueError("repo_url must be public HTTPS GitHub URL...")
           return v

       @field_validator("run_tests")
       def reject_run_tests(cls, v):
           if v: raise ValueError("run_tests not yet supported (v1.1)")
           return v

       @field_validator("task")
       def task_len(cls, v):
           if len(v) > 8000: raise ValueError("task length > 8000 chars")
           return v
   ```
   Plus `JobResponse` mirroring db row (with `diff_blob` truncated to 1MB + `diff_truncated` flag).

3. **CRUD** (`db/crud/jobs.py`): all functions take session + return job model. `mark_running(id)`, `mark_succeeded(id, diff, files, summary, exit_code)`, `mark_failed(id, error)`, `mark_cancelled(id)`, `get(id, user_id)` (returns None on owner mismatch), `list_orphans()` (status=running on worker boot).

4. **Route POST** (`routes/jobs.py`):
   - Auth dep → user_id
   - Validate via `CreateJobRequest`
   - Insert row status=queued
   - `await arq_pool.enqueue_job("run_codex_job", job_id=..., repo_url=..., branch=..., task=..., mode=..., timeout=req.timeout_seconds or settings.JOB_TIMEOUT_SECONDS, _queue_name=settings.ARQ_QUEUE_NAME)`
   - Return 202 + `JobCreatedResponse`

5. **Route GET `{id}`**:
   - Fetch via crud; 404 if missing; 403 if owner mismatch
   - Truncate diff_blob to 1MB before serializing
   - Return JobResponse

6. **Route DELETE `{id}`**:
   - Fetch via crud
   - If queued: `await arq_pool.abort_job(arq_job_id)` (arq supports this); mark cancelled
   - If running: `await redis.set(f"cancel:job:{id}", "1", ex=300)`
   - Return current state (always 200; idempotent)

7. **Route GET `{id}/events`** (SSE):
   - Headers (rate-limit etc.) MUST be set in `EventSourceResponse(headers=...)` from this route layer, NOT injected via `BaseHTTPMiddleware` (Starlette buffers SSE — see phase-03 §C3 master pattern).
   - Wrap inner generator with `sse_helpers.keepalive_wrap(..., interval=15.0)` (introduced phase-00) to emit `: keepalive\n\n` comments through Caddy idle timeout for jobs > 15s.
   - Detect `request.is_disconnected()` between events; on disconnect, exit generator (resources freed by `finally:` blocks).
   ```python
   @router.get("/v1/codex/jobs/{id}/events")
   async def stream_events(id, request: Request, user=Depends(auth)):
       job = await crud.get(id, user.id)
       if not job: raise HTTPException(404)
       headers = request.state.rate_limit_headers  # set by RateLimitMiddleware (phase-06 contract)
       inner = _replay_then_subscribe(id, request)
       return EventSourceResponse(
           sse_helpers.keepalive_wrap(inner, interval=15.0),
           headers=headers,
           ping=15,  # belt-and-suspenders; helper also emits
       )

   async def _replay_then_subscribe(id, request):
       backlog = await redis.lrange(f"job:events:list:{id}", 0, -1)
       for raw in backlog:
           if await request.is_disconnected(): return
           evt = json.loads(raw)
           yield {"event": evt["type"], "data": raw.decode()}
           if evt["type"] in TERMINAL_TYPES: return
       # subscribe live
       async with redis.pubsub() as ps:
           await ps.subscribe(f"job:events:{id}")
           async for msg in ps.listen():
               if await request.is_disconnected(): return
               if msg["type"] != "message": continue
               evt = json.loads(msg["data"])
               yield {"event": evt["type"], "data": msg["data"].decode()}
               if evt["type"] in TERMINAL_TYPES: return
   ```

8. **Arq worker entry** (`workers/arq_worker.py`):
   ```python
   class WorkerSettings:
       functions = [run_codex_job]
       redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
       queue_name = settings.ARQ_QUEUE_NAME
       max_jobs = settings.ARQ_MAX_JOBS
       on_startup = startup_hook       # cleanup orphans
       on_shutdown = shutdown_hook
       job_timeout = settings.JOB_TIMEOUT_SECONDS + 60  # arq hard kill > our soft timeout
   ```

9. **Handler** (`workers/job_handlers.py`):
   ```python
   async def run_codex_job(ctx, job_id, repo_url, branch, task, mode, timeout):
       redis, db = ctx["redis"], ctx["db"]
       publisher = EventPublisher(redis, db, job_id)
       # early-cancel race
       if await redis.get(f"cancel:job:{job_id}"):
           await crud.mark_cancelled(db, job_id)
           await publisher.publish("job.cancelled", {"reason": "cancelled_before_start"})
           return
       await crud.mark_running(db, job_id)
       await publisher.publish("job.started", {})
       workspace = create_workspace(job_id)
       try:
           async with asyncio.timeout(timeout):
               await git_clone.clone(repo_url, branch, f"{workspace}/repo", timeout=settings.GIT_CLONE_TIMEOUT)
               head_before = await git_clone.rev_parse_head(f"{workspace}/repo")
               last_msg = ""
               async for evt in runner.stream(task=task, cwd=f"{workspace}/repo", mode=mode):
                   if await redis.get(f"cancel:job:{job_id}"):
                       runner.terminate()
                       raise asyncio.CancelledError("cancelled by user")
                   await publisher.publish("job.codex_event", evt)
                   if evt.get("type") == "item.completed" and evt["item"].get("type") == "agent_message":
                       last_msg = evt["item"]["text"]
               diff, files = await git_diff.capture(f"{workspace}/repo", head_before)
               await publisher.publish("job.diff_ready", {"files_changed": files})
               await crud.mark_succeeded(db, job_id, diff, files, last_msg, exit_code=0)
               await publisher.publish("job.completed", {"summary": last_msg})
       except asyncio.CancelledError:
           await crud.mark_cancelled(db, job_id)
           await publisher.publish("job.cancelled", {})
       except asyncio.TimeoutError:
           await crud.mark_failed(db, job_id, "timeout")
           await publisher.publish("job.failed", {"error": "timeout"})
       except Exception as e:
           await crud.mark_failed(db, job_id, str(e))
           await publisher.publish("job.failed", {"error": str(e)[:500]})
       finally:
           cleanup_workspace(workspace)
   ```

10. **`git_clone.py`**: `asyncio.create_subprocess_exec("git", "clone", "--depth", "1", "-b", branch, repo_url, dest, stdout=PIPE, stderr=PIPE)`; wait_for with timeout; on failure capture stderr (first 1000 chars) and raise `GitCloneError`. Re-validate URL just before exec (defense in depth against TOCTOU on db row).

11. **`git_diff.py`**: `git -C <repo> diff <head_before> HEAD --` for blob; `git -C <repo> diff --name-only <head_before> HEAD` for files; `git -C <repo> ls-files --others --exclude-standard` for untracked → append to files list. Cap blob at 16 MB before persisting (truncate + mark `diff_size_bytes` real, set `diff_truncated=true` if hit cap).

12. **`event_publisher.py`**:
    ```python
    async def publish(self, type, data):
        payload = {"type": type, "job_id": self.job_id, "ts": now_iso(), "data": data}
        raw = json.dumps(payload)
        async with self.redis.pipeline() as pipe:
            pipe.rpush(f"job:events:list:{self.job_id}", raw)
            pipe.expire(f"job:events:list:{self.job_id}", 86400)  # 24h TTL
            pipe.publish(f"job:events:{self.job_id}", raw)
            await pipe.execute()
        await crud.append_audit(self.db, self.job_id, type, data)
    ```

13. **Orphan recovery** (`startup_hook`): on worker boot, `crud.list_orphans()` → for each, `mark_failed(reason="worker_restarted")` + publish `job.failed`. Run before functions registered.

14. **Tests**:
    - `test_jobs_schema.py`: reject private repo URL, SSH URL, run_tests=true, task length > 8000.
    - `test_git_clone_url_validation.py`: regex matches owner/repo and `.git` suffix variants.
    - `test_jobs_lifecycle.py`: TestClient POST → poll GET until `succeeded` → assert diff_blob non-empty + files_changed populated. Uses fake codex runner.
    - `test_jobs_cancel.py`: spawn long-running fake runner → DELETE during run → assert status=`cancelled`, workspace dir cleaned, runner subprocess reaped.

## Todo List
- [ ] Alembic migration: jobs table + indexes
- [ ] `Job` SQLAlchemy model in `db/models.py`
- [ ] `db/crud/jobs.py` with all state transitions
- [ ] `schemas/openai_jobs.py` with public-only URL validator
- [ ] `routes/jobs.py`: POST/GET/DELETE/events
- [ ] `workers/arq_worker.py` WorkerSettings + lifecycle hooks
- [ ] `workers/job_handlers.py` `run_codex_job`
- [ ] `workers/git_clone.py` with timeout + stderr capture
- [ ] `workers/git_diff.py` with 16MB cap
- [ ] `workers/event_publisher.py` (RPUSH+PUBLISH+audit pipelined)
- [ ] Orphan recovery on worker boot
- [ ] Settings additions (ARQ_*, GIT_CLONE_TIMEOUT)
- [ ] Unit tests pass
- [ ] Integration tests pass
- [ ] Manual smoke: `curl POST /v1/codex/jobs` → poll GET → cancel runs

## Success Criteria
- POST returns 202 + UUID job id within 50ms (no blocking work).
- A real public repo (small fixture, e.g. a hello-world repo) clones + runs codex + produces non-empty diff in test environment within 5 min.
- DELETE during running state: `kill -0 <subprocess_pid>` returns nonzero within `JOB_CANCEL_GRACE_SECONDS+1`.
- Workspace `/workspaces/{id}` removed after every terminal state (assert via integration test).
- Late SSE subscriber (subscribe AFTER job complete) gets full replay then close.
- Live SSE subscriber sees events in order with no gaps vs Redis list contents.
- Worker crash mid-job: on restart, that row is `failed` (not stuck `running`).
- Private repo URL → 422 with explicit error.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Codex doesn't emit final event on cancel | High | Med | Cancel flag is authoritative; wrapper marks state independent of codex output (researcher-01 §8) |
| Git clone hangs on flaky network | Med | High | 60s subprocess timeout; on hit → `mark_failed("clone_timeout")` |
| Worker process killed mid-job → orphan workspace | Med | Med | Startup orphan-recovery sweep + cron janitor (phase 08) |
| Redis pub/sub message loss between RPUSH and SUBSCRIBE | Low | Med | Replay-then-subscribe pattern: subscriber LRANGEs first, never relies on live stream alone |
| Diff blob > 16 MB OOM in worker | Low | High | Stream `git diff` to file, then read with size cap; truncate + flag |
| Arq queue backed up → SLA breach | Med | Med | Per-tier concurrent cap (phase 06) at enqueue time; 429 if tier already at limit |
| Cancel flag TTL expires before worker reads | Low | Med | TTL 300s vs poll interval 1s; worst case worker misses cancel for queued jobs only — guarded by early-cancel check at handler start |
| Path traversal via repo_url | Low | High | URL regex limits to GitHub HTTPS; workspace path computed via `WORKSPACE_ROOT/{uuid}` not user input |
| Symlink in cloned repo escapes workspace | Med | High | `git clone` doesn't follow symlinks at clone time; codex sandbox `workspace-write` enforces Landlock/Seatbelt boundary (phase 02) |

## Security Considerations
- `repo_url` regex bans non-GitHub hosts and SSH URLs; defense vs SSRF (no localhost/internal hosts cloneable).
- Workspace dir is `os.path.join(WORKSPACE_ROOT, str(uuid))`; `realpath` check that result is under `WORKSPACE_ROOT` before any write.
- Worker runs codex with `--sandbox workspace-write` (mode=workspace-write) or `--sandbox read-only` (mode=read-only); never `danger-full-access`.
- Cancel endpoint requires same `user_id` as job creator (auth + ownership check before SET cancel flag).
- `task` field contents logged at INFO with truncation to 200 chars in non-redacted log fields; full task only in `audit_log` table (DB ACL controlled).
- Diff content may contain user-uploaded secrets; do NOT pass diff_blob to structlog — only file_changed list and exit_code go to logs.
- `cancel:job:{id}` Redis key has 5-min TTL to bound abuse via flag-pollution; key is per-job (no user-controlled key composition).

## Next Steps
- Phase 06 layers per-tier concurrent cap on top of POST `/v1/codex/jobs` (enqueue rejected with 429 if tier already at limit).
- Phase 07 observability: jobs.duration histogram, jobs.queue_depth gauge, codex_subprocess.exit_code counter.
- Phase 08 hardening: workspace janitor cron (sweeps stale dirs > 1h with no live job), diff blob offload to S3/MinIO when > 1 MB.
- v1.1: private repo support (PAT or GitHub App), webhook callbacks on terminal state, run_tests flag.
