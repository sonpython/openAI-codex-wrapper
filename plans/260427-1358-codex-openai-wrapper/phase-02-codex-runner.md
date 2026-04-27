# Phase 02: Codex Runner

## Context Links
- Brainstorm: `../reports/brainstorm-260427-1358-codex-openai-wrapper.md` (§3 architecture, §6 JSONL→OpenAI mapping, §7 risks subprocess/workspace/JSONL)
- Phase 00: `phase-00-bootstrap.md` (provides `CODEX_BIN`, `CODEX_AUTH_DIR`, `WORKSPACE_ROOT`, `JOB_TIMEOUT_SECONDS`, `JOB_CANCEL_GRACE_SECONDS`)
- JSONL schema: `research/researcher-01-codex-jsonl-schema.md` (§1-2 events/items, §3 errors, §4 CLI flags, §7 stdout/stderr, §8 timeout, §9 sandbox)
- OpenAI taxonomy (downstream consumer): `research/researcher-02-openai-event-taxonomy.md`
- Project rules: `../../.claude/rules/development-rules.md`

## Overview
- Priority: critical
- Status: pending
- Effort: M
- Description: Build the core subprocess wrapper that turns `codex exec --json` output into a Python async iterator of typed events. This phase delivers four reusable modules — `runner` (subprocess lifecycle), `jsonl_parser` (event types + tolerant decoding), `workspace` (per-job ephemeral dir + path-traversal guard), and `auth_session` (`~/.codex` health check) — plus a background lifespan poller that gates request acceptance on Codex session validity. Phases 03 (chat-completions), 04 (responses), and 05 (jobs) all consume this layer.

## Red Team Resolutions
- **C1** — `--ephemeral` usage now explicitly gated on phase-00 `make verify-codex`. Documented fallback path: if 0.125.0 doesn't accept `--ephemeral`, runner uses per-request tmpdir + `--cd` only (no session persistence claim).
- **C6** — `validate_path_inside` rewritten using `os.path.realpath` + `os.path.commonpath` (single-resolution, race-aware). Explicit doc that this is best-effort defense-in-depth; primary control is Codex sandbox `workspace-write` enforcing Landlock/Seatbelt at kernel level.
- **C11** — Added §Key Insights note: phase-00 `verify-codex.sh` warns if 0.125.0 advertises unix-socket transport; phase-02 follow-up TODO covers fallback flag (likely `--io stdio` or env var).
- **C3 (cross-ref)** — Runner does NOT emit SSE itself; route layer (phase 03/04) wraps `run_codex` with `sse_helpers.keepalive_wrap`. Runner yield cadence (one event per JSONL line) feeds into the keepalive timer — documented that long agent_message gaps are normal and keepalive layer handles them.

## Key Insights
- Per researcher-01 §7 + issue #15451: stdout under `--json` should be pure JSONL, but MCP tool output can contaminate it. **Defense:** only attempt `json.loads` on lines whose first non-whitespace byte is `{`. Everything else → debug log. Never crash the stream on a stray non-JSON line.
- Per researcher-01 §1-2: actual events use `type: "agent_message"` (not `"assistant_message"` as some docs show). Pydantic discriminator must accept both for forward-compat — use a union with `Literal["agent_message","assistant_message"]` aliased to one internal kind. Brainstorm §6 already maps both to `delta.content`.
- Per researcher-01 §8: SIGTERM behavior in non-interactive mode is partially documented; we must implement two-stage termination (SIGTERM → wait `JOB_CANCEL_GRACE_SECONDS` → SIGKILL). Don't trust Codex to emit a final event on cancel; the runner caller must treat iterator-stop as definitive.
- Per researcher-01 §9 + brainstorm §7 path-traversal risk: `validate_path_inside(workspace, target)` uses `os.path.realpath` once + `os.path.commonpath` comparison. Replaces earlier "walk parts + reject symlinks" loop because `target.relative_to(workspace)` raises `ValueError` on absolute paths outside workspace — caller wouldn't catch correctly. New algorithm is straight-line and cannot raise the wrong exception type (C6 fix). **Important**: this is best-effort defense-in-depth; primary control is Codex `--sandbox workspace-write` enforcing Landlock (Linux 5.13+) / Seatbelt (macOS) at the kernel level. Application check guards against logic bugs, NOT malicious post-resolution symlink swaps (TOCTOU).
- Per researcher-01 §6 changelog hint ("Unix socket transport, reasoning-token usage, rollout tracing" in 0.125.0): phase-00 `verify-codex.sh` step 4 warns if `unix-socket` / `--io` flags appear in `codex exec --help`. If they do, this phase MUST add a verification step that stdout JSONL still works under the default invocation, and document fallback flag (probably `--io stdio` or `CODEX_TRANSPORT=stdio` env). v1 default-deny if stdout JSONL doesn't work: refuse to start, escalate.
- Per researcher-01 §5: `~/.codex/auth.json` may include `expires_at`. We won't trust the timestamp blindly; `codex auth status` (or `codex login status` per §5) is authoritative. Fallback if CLI subcommand absent: parse `auth.json` mtime + `expires_at`. Background poller every 5 min sets `app.state.codex_session_healthy: bool`.
- KISS: `run_codex` does not own retries, token accounting, or OpenAI shape conversion — those are caller concerns. Single responsibility: spawn → stream typed events → cleanup.
- File-size budget: parser file will be the biggest. Split pydantic event models into a dedicated `events.py` if `jsonl_parser.py` exceeds 200 LOC.

## Requirements

### Functional
- `async def run_codex(prompt, *, allow_write, workspace_dir, timeout, model=None, search=False) -> AsyncIterator[CodexEvent]`:
  - Spawns `codex exec --json --color never --ephemeral --skip-git-repo-check --cd <workspace_dir> --sandbox <read-only|workspace-write> --ask-for-approval never <prompt-via-stdin-or-arg>`.
  - Yields one parsed `CodexEvent` per JSONL line in stdout.
  - Honors caller `cancel()` (Python: closing the async generator) → SIGTERM → grace → SIGKILL.
  - Honors `timeout` (seconds) → on overflow: SIGTERM escalation + yields one synthesized `ErrorEvent(code="TIMEOUT")` then closes.
  - On non-zero exit + no error event → synthesize `ErrorEvent(code="EXIT_NONZERO", message=stderr_tail)`.
- JSONL parser handles all event types in researcher-01 §1 (12 types) and §2 (10 item types) with strict-but-tolerant semantics: unknown event/item types → log at debug + skip (don't raise).
- `make_workspace(job_id) -> Path` creates `WORKSPACE_ROOT/<job_id>/` with mode `0o700`; raises if `WORKSPACE_ROOT` doesn't exist.
- `cleanup_workspace(path)` recursively deletes; idempotent; never escapes `WORKSPACE_ROOT`.
- `validate_path_inside(workspace, target) -> Path` returns resolved target if safe; raises `WorkspaceTraversalError` if symlinks present or path escapes.
- `verify_codex_session() -> tuple[bool, datetime|None]` runs `codex auth status` with 3s timeout; on success returns `(True, expires_at_or_None)`; on failure tries `auth.json` parse fallback.
- Lifespan background task polls `verify_codex_session()` every 5 min; sets `app.state.codex_session_healthy`; emits `WARN` log on first transition healthy→unhealthy.
- `/readyz` (extended from phase 00) now also checks `app.state.codex_session_healthy` — returns 503 if false.
- stderr captured into a bounded ring buffer (cap 64 KiB); included in `ErrorEvent` when needed.

### Non-Functional
- Each Python file ≤ 200 LOC. Anticipated splits: `codex/runner.py`, `codex/jsonl_parser.py`, `codex/events.py` (pydantic models if parser overflows), `codex/workspace.py`, `codex/auth_session.py`, `codex/exceptions.py`.
- Async-only. No `subprocess.Popen`; use `asyncio.create_subprocess_exec`.
- No reliance on shell — args passed as a list (avoid quoting bugs + injection).
- Subprocess group / process group: spawn with `start_new_session=True` so SIGTERM hits child + descendants on POSIX.
- Memory bounded: stderr capped at 64 KiB; stdout streamed line-by-line (no full buffer).
- Test coverage: unit tests with fixture JSONL files cover every event type from researcher-01 §1-2.

## Architecture

```
caller (chat / responses / jobs handler)
        │
        │ async for evt in run_codex(...):
        ▼
┌─────────────────────────────────────────────────────────────┐
│ codex/runner.py                                             │
│                                                             │
│   run_codex(...) ─► make_workspace() ─► spawn subprocess    │
│                                          │                  │
│                                  ┌───────┴────────┐         │
│                                  ▼                ▼         │
│                          stdout reader      stderr reader   │
│                          (JSONL pipe)       (ring buffer)   │
│                                  │                          │
│                                  ▼                          │
│                          jsonl_parser.parse_line()          │
│                                  │                          │
│                                  ▼                          │
│                            yield CodexEvent ────────► caller│
│                                                             │
│   on cancel/timeout: SIGTERM → wait(grace) → SIGKILL        │
│   on exit:           drain → synth ErrorEvent if abnormal   │
│   finally:           cleanup_workspace()                    │
└─────────────────────────────────────────────────────────────┘

lifespan background task:
   every 300s → verify_codex_session() → app.state.codex_session_healthy
   /readyz reads app.state.codex_session_healthy
```

Data flow per request (chat-completion example, downstream of phase 03):
```
HTTP req ─► build_prompt(messages) ─► make_workspace(job_id=uuid)
        ─► run_codex(prompt, allow_write=False, ...)
        ─► for each event: map to OpenAI chunk (phase 03)
        ─► cleanup_workspace() in finally
```

## Related Code Files

### To create
- `src/codex/__init__.py`
- `src/codex/exceptions.py` (`CodexRunnerError`, `WorkspaceTraversalError`, `CodexSessionUnhealthy`; ≤ 50 LOC)
- `src/codex/events.py` (pydantic event/item models — split target ≤ 200 LOC)
- `src/codex/jsonl_parser.py` (line → event dispatcher; ≤ 150 LOC)
- `src/codex/runner.py` (subprocess orchestration; ≤ 200 LOC)
- `src/codex/workspace.py` (mkdir/cleanup/path safety; ≤ 120 LOC)
- `src/codex/auth_session.py` (session probe + lifespan poller; ≤ 150 LOC)
- `tests/unit/test_jsonl_parser.py` (per-event-type fixtures)
- `tests/unit/test_workspace.py` (traversal cases)
- `tests/unit/test_auth_session.py` (mock subprocess)
- `tests/integration/test_runner_smoke.py` (real `codex exec --json` with trivial prompt — guarded by `CODEX_AUTH_DIR` presence; skipped in CI without secret)
- `tests/fixtures/jsonl/*.jsonl` (canned event streams: thread_started, agent_message, command_execution, error, turn_completed, etc.)

### To modify
- `src/gateway/app.py` — extend lifespan to start `auth_session.start_poller(app)` task and cancel it on shutdown.
- `src/gateway/health.py` — `/readyz` reads `app.state.codex_session_healthy`.
- `src/settings.py` — add `CODEX_SESSION_POLL_INTERVAL_SECONDS: int = 300`, `CODEX_AUTH_PROBE_TIMEOUT_SECONDS: int = 3`.
- `.env.example` — document the two new vars.

### To delete
- (none)

## Implementation Steps

1. **Exceptions** (`src/codex/exceptions.py`)
   - `class CodexRunnerError(Exception)`, `class WorkspaceTraversalError(CodexRunnerError)`, `class CodexSessionUnhealthy(CodexRunnerError)`. Empty bodies; rely on `args[0]` for message.

2. **Event models** (`src/codex/events.py`) — per researcher-01 §1-2. Use pydantic discriminated unions on `type`.
   ```python
   class _Base(BaseModel): model_config = ConfigDict(extra="allow")  # tolerate forward-compat fields

   class ThreadStarted(_Base):
       type: Literal["thread.started"]
       thread_id: str

   class TurnStarted(_Base):
       type: Literal["turn.started"]
       turn_id: str | None = None

   # ---- item payloads ----
   class AgentMessageItem(_Base):
       type: Literal["agent_message", "assistant_message"]  # accept both per researcher-01 §2 note
       id: str
       text: str

   class ReasoningItem(_Base):
       type: Literal["reasoning"]; id: str; text: str | None = None
   class CommandExecutionItem(_Base):
       type: Literal["command_execution"]; id: str; command: str; status: str | None = None
   class FileChangeItem(_Base):
       type: Literal["file_change"]; id: str; path: str; status: str | None = None
   class FileReadItem(_Base):
       type: Literal["file_read"]; id: str; path: str
   class ToolUseItem(_Base):
       type: Literal["tool_use"]; id: str; name: str; arguments: dict | None = None
   class ToolResultItem(_Base):
       type: Literal["tool_result"]; id: str; result: Any | None = None
   class WebSearchItem(_Base):
       type: Literal["web_search"]; id: str; query: str | None = None
   class McpServerStartupItem(_Base):
       type: Literal["mcp_server_startup"]; id: str
   class PlanUpdateItem(_Base):
       type: Literal["plan_update"]; id: str

   ItemPayload = Annotated[
       Union[AgentMessageItem, ReasoningItem, CommandExecutionItem, FileChangeItem,
             FileReadItem, ToolUseItem, ToolResultItem, WebSearchItem,
             McpServerStartupItem, PlanUpdateItem],
       Field(discriminator="type")]

   class ItemStarted(_Base): type: Literal["item.started"]; item: ItemPayload
   class ItemCompleted(_Base): type: Literal["item.completed"]; item: ItemPayload
   class ItemUpdated(_Base): type: Literal["item.updated"]; item: ItemPayload

   class TokenUsage(_Base):
       input_tokens: int = 0; cached_input_tokens: int = 0
       output_tokens: int = 0; reasoning_tokens: int = 0

   class TurnCompleted(_Base):
       type: Literal["turn.completed"]; usage: TokenUsage | None = None
   class TurnFailed(_Base):
       type: Literal["turn.failed"]; usage: TokenUsage | None = None
       error: dict | None = None

   class ErrorPayload(_Base):
       code: str; message: str; details: dict | None = None
   class ErrorEvent(_Base):
       type: Literal["error"]; error: ErrorPayload

   CodexEvent = Annotated[
       Union[ThreadStarted, TurnStarted, ItemStarted, ItemCompleted, ItemUpdated,
             TurnCompleted, TurnFailed, ErrorEvent],
       Field(discriminator="type")]
   ```

3. **Parser** (`src/codex/jsonl_parser.py`)
   ```python
   _adapter = TypeAdapter(CodexEvent)

   def parse_line(line: str) -> CodexEvent | None:
       s = line.lstrip()
       if not s.startswith("{"):
           logger.debug("codex.stdout.non_json", raw=line[:200])
           return None  # MCP contamination guard, researcher-01 #15451
       try:
           obj = json.loads(s)
       except json.JSONDecodeError:
           logger.warning("codex.stdout.bad_json", raw=line[:200])
           return None
       try:
           return _adapter.validate_python(obj)
       except ValidationError as e:
           logger.debug("codex.event.unknown_or_invalid", type=obj.get("type"), err=str(e)[:200])
           return None
   ```
   `extra="allow"` on base means new fields don't fail; unknown TOP-LEVEL `type` falls through to None.

4. **Workspace** (`src/codex/workspace.py`)
   ```python
   def make_workspace(job_id: str) -> Path:
       root = Path(settings.WORKSPACE_ROOT)
       if not root.is_dir(): raise CodexRunnerError(f"WORKSPACE_ROOT {root} missing")
       wd = root / str(job_id)
       wd.mkdir(mode=0o700, parents=False, exist_ok=False)
       return wd

   def cleanup_workspace(path: Path) -> None:
       root = Path(settings.WORKSPACE_ROOT).resolve()
       p = path.resolve()
       if not str(p).startswith(str(root) + os.sep): return  # never escape; idempotent
       shutil.rmtree(p, ignore_errors=True)

   # Addresses C6: realpath-once + commonpath; never raises ValueError.
   # Best-effort defense-in-depth — Codex `--sandbox workspace-write` (Landlock/Seatbelt)
   # is the primary control; this guards against application logic bugs, not malicious
   # codex-output post-resolution symlink swaps (TOCTOU is acknowledged + accepted).
   def validate_path_inside(workspace: Path, target: Path) -> Path:
       ws_real = os.path.realpath(workspace)
       try:
           tgt_real = os.path.realpath(target)
       except OSError as e:
           raise WorkspaceTraversalError(f"unresolvable target: {e}")
       # commonpath returns ws_real iff tgt_real is at or under ws_real
       try:
           common = os.path.commonpath([tgt_real, ws_real])
       except ValueError as e:
           # different drives on Windows etc — treat as escape
           raise WorkspaceTraversalError(f"incomparable paths: {e}")
       if common != ws_real:
           raise WorkspaceTraversalError(f"target {tgt_real} not inside {ws_real}")
       return Path(tgt_real)
   ```
   Unit tests: relative escape (`../../etc/passwd`), absolute outside (e.g. `Path("/etc/passwd")`), symlink-to-parent inside workspace (resolved away by realpath; check still trips), valid nested path. **No test relies on a ValueError-vs-WorkspaceTraversalError distinction**; only `WorkspaceTraversalError` is raised on bad input.

5. **Runner** (`src/codex/runner.py`) — pseudocode (final ≤ 200 LOC).

   **Dependency**: phase-00 `make verify-codex` MUST have passed. That script asserts `--ephemeral` exists in 0.125.0 (C1). If verify-codex reports `--ephemeral` MISSING (future-proofing), runner falls back to per-request tmpdir + `--cd` ONLY (no `--ephemeral` flag, no session-persistence claim). Implement both branches behind a startup-time `settings.CODEX_HAS_EPHEMERAL: bool` (auto-set during verify-codex; defaults False so we fail-safe).

   ```python
   async def run_codex(prompt: str, *, allow_write: bool, workspace_dir: Path,
                      timeout: float, model: str | None = None,
                      search: bool = False) -> AsyncIterator[CodexEvent]:
       sandbox = "workspace-write" if allow_write else "read-only"
       argv = [settings.CODEX_BIN, "exec", "--json", "--color", "never",
               "--skip-git-repo-check",
               "--cd", str(workspace_dir),
               "--sandbox", sandbox,
               "--ask-for-approval", "never"]
       if settings.CODEX_HAS_EPHEMERAL:           # gated by phase-00 verify-codex
           argv.insert(4, "--ephemeral")          # before --skip-git-repo-check
       if model: argv += ["-m", model]
       if search: argv += ["--search"]
       argv.append(prompt)  # alternative: pipe via stdin if prompt > 8 KB

       proc = await asyncio.create_subprocess_exec(
           *argv,
           stdout=asyncio.subprocess.PIPE,
           stderr=asyncio.subprocess.PIPE,
           start_new_session=True,
           env={**os.environ, "HOME": settings.CODEX_AUTH_DIR_PARENT})  # so codex finds ~/.codex
       stderr_buf = bytearray()
       stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_buf, cap=64*1024))

       saw_terminal = False
       try:
           async with asyncio.timeout(timeout):
               async for raw in proc.stdout:
                   line = raw.decode("utf-8", errors="replace")
                   evt = parse_line(line)
                   if evt is None: continue
                   if isinstance(evt, (TurnCompleted, TurnFailed, ErrorEvent)): saw_terminal = True
                   yield evt
       except asyncio.TimeoutError:
           await _terminate(proc, settings.JOB_CANCEL_GRACE_SECONDS)
           yield ErrorEvent(type="error", error=ErrorPayload(code="TIMEOUT",
                            message=f"exceeded {timeout}s"))
           return
       except (asyncio.CancelledError, GeneratorExit):
           await _terminate(proc, settings.JOB_CANCEL_GRACE_SECONDS)
           raise
       finally:
           stderr_task.cancel()
           with contextlib.suppress(BaseException): await proc.wait()

       rc = proc.returncode
       if rc != 0 and not saw_terminal:
           tail = stderr_buf[-4096:].decode("utf-8", errors="replace")
           yield ErrorEvent(type="error", error=ErrorPayload(
               code="EXIT_NONZERO", message=f"codex exited {rc}",
               details={"stderr_tail": tail}))

   async def _terminate(proc, grace: float) -> None:
       if proc.returncode is not None: return
       try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
       except ProcessLookupError: return
       try: await asyncio.wait_for(proc.wait(), timeout=grace)
       except asyncio.TimeoutError:
           with contextlib.suppress(ProcessLookupError):
               os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
   ```
   Caller is responsible for `make_workspace` / `cleanup_workspace` (KISS — runner doesn't own lifecycle of dir it didn't create).

6. **Auth session** (`src/codex/auth_session.py`)
   - `async def verify_codex_session() -> tuple[bool, datetime | None]`:
     1. Try `await asyncio.create_subprocess_exec(settings.CODEX_BIN, "auth", "status", ...)` with `CODEX_AUTH_PROBE_TIMEOUT_SECONDS`. Exit 0 → healthy. Some versions name it `login status` — try both, accept first non-128 exit.
     2. Fallback: parse `<CODEX_AUTH_DIR>/auth.json`. If `expires_at` future → `(True, expires_at)`; else `(False, expires_at)`.
   - `async def start_poller(app: FastAPI) -> asyncio.Task`:
     - Sets `app.state.codex_session_healthy = True` initially (optimistic — set False if first probe fails).
     - Loops: probe → update state → on transition WARN log → `await asyncio.sleep(settings.CODEX_SESSION_POLL_INTERVAL_SECONDS)`.
     - Cancellation-safe: catches `CancelledError`, returns.

7. **Lifespan wiring** (`src/gateway/app.py`)
   - In existing lifespan: after Redis init, `app.state.codex_session_healthy = False` (default-deny); `app.state._codex_poll_task = await auth_session.start_poller(app)`.
   - On shutdown: cancel task, await with suppress.

8. **Readiness extension** (`src/gateway/health.py`)
   - `/readyz` returns 503 with `{"status":"not_ready","reason":"codex_session_unhealthy"}` when `app.state.codex_session_healthy` is False.

9. **Tests**
   - **Parser unit**: feed each event-type fixture from `tests/fixtures/jsonl/` — assert correct subclass returned. Feed garbage line → returns None. Feed unknown `type` → returns None + debug log.
   - **Workspace unit**: traversal cases (above). `make_workspace` collision on duplicate id → `FileExistsError`.
   - **Runner unit (mocked)**: monkeypatch `asyncio.create_subprocess_exec` with a fake proc whose stdout yields canned bytes; assert event sequence + cleanup of stderr task. Test SIGTERM path: feed slow proc + small timeout, assert `TIMEOUT` ErrorEvent yielded.
   - **Auth session unit**: monkeypatch subprocess to return 0/non-0; verify state transitions; verify auth.json fallback parses.
   - **Integration smoke** (`tests/integration/test_runner_smoke.py`): real codex CLI with prompt `"reply with the single word: pong"`. Skipped if `not Path(settings.CODEX_AUTH_DIR + "/auth.json").exists()`. Asserts ≥ 1 `ItemCompleted(AgentMessageItem)` and a `TurnCompleted`.

10. **Local verification**
    - `pytest tests/unit/test_jsonl_parser.py -q` covers all 12 event types + 10 item types.
    - Bring up compose with valid `~/.codex` mount; `curl /readyz` → 200; remove auth.json; wait 5+ min OR force re-poll → `/readyz` → 503.
    - Manual: `python -c "import asyncio; from src.codex.runner import run_codex; ..."` runs a one-shot prompt and prints events.

## Todo List
- [ ] `src/codex/exceptions.py`
- [ ] `src/codex/events.py` pydantic models (12 events × 10 item types) with both `agent_message`/`assistant_message`
- [ ] `src/codex/jsonl_parser.py` with `{`-prefix guard + tolerant validate
- [ ] `src/codex/workspace.py` make / cleanup / validate (realpath+commonpath, raises only `WorkspaceTraversalError`)
- [ ] `src/codex/runner.py` subprocess orchestration + SIGTERM/SIGKILL escalation + stderr ring buffer + `CODEX_HAS_EPHEMERAL` branch
- [ ] Runner depends on `make verify-codex` from phase-00 having passed (gate before merge)
- [ ] `src/codex/auth_session.py` probe + poller
- [ ] Settings: `CODEX_SESSION_POLL_INTERVAL_SECONDS`, `CODEX_AUTH_PROBE_TIMEOUT_SECONDS`
- [ ] Lifespan: start/stop poller; default `codex_session_healthy=False`
- [ ] `/readyz` checks codex session
- [ ] JSONL fixtures for every event/item type
- [ ] Unit tests (parser, workspace, runner mocked, auth probe)
- [ ] Integration smoke test (skipped without auth)
- [ ] Manual: timeout case + cancel case verified

## Success Criteria
- All 12 event types + 10 item types from researcher-01 §1-2 round-trip through the parser.
- Garbage / non-JSON / unknown-type lines never raise; only debug-logged.
- `run_codex` cancellation propagates SIGTERM within `JOB_CANCEL_GRACE_SECONDS` and SIGKILL after; no zombie processes after 100 cycles.
- Timeout produces a synthesized `ErrorEvent(code="TIMEOUT")` and closes cleanly.
- Workspace traversal test cases all rejected (`../../etc/passwd`, absolute outside, symlink to parent).
- Removing `~/.codex/auth.json` flips `app.state.codex_session_healthy` → False within next poll; `/readyz` returns 503.
- Integration smoke (when auth available) returns ≥ 1 agent_message item and a turn_completed.
- Each Python file ≤ 200 LOC. ruff + mypy clean.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MCP output contaminates stdout (researcher-01 #15451) | M | M | `{`-prefix guard + JSONDecodeError catch; no crash, only debug log. |
| JSONL schema drift across codex versions | M | M | Pin `@openai/codex@0.125.0` (phase 0); pydantic `extra="allow"` tolerates new fields; CI integration test runs every PR. |
| SIGTERM doesn't kill child processes (subshells) | M | M | `start_new_session=True` + `os.killpg` targets the whole group. SIGKILL fallback after grace. |
| `~/.codex` mount stale / read-only-fails-write | L | HIGH | Read-only mount (brainstorm §3); session refresh handled on host out-of-band. Poller surfaces unhealthy state fast. |
| Path traversal via symlink swap during run | L | HIGH | Sandbox `workspace-write` (Landlock/Seatbelt) is primary defense; `validate_path_inside` is application-layer belt-and-braces. **Addressed via C6**: rewrite uses `os.path.realpath` + `os.path.commonpath` (single-resolution; no ValueError leak). Documented as defense-in-depth, not race-free. |
| `--ephemeral` flag missing from 0.125.0 | L | HIGH | **Addressed via C1**: `make verify-codex` (phase-00) asserts flag presence; runner reads `settings.CODEX_HAS_EPHEMERAL` and falls back to plain `--cd` if absent. Bootstrap fails fast if check fails. |
| 0.125.0 introduces unix-socket transport, breaking stdout pipe | L | HIGH | **Addressed via C11**: `verify-codex.sh` step 4 greps help for `unix.?socket` / `--io`; if found, raises a follow-up TODO before phase-02 implementation begins; v1 default-deny if stdout JSONL doesn't work. |
| Long stderr fills memory | M | L | 64 KiB ring buffer cap; older bytes overwritten. |
| Probe `codex auth status` subcommand renamed | L | M | Try both `auth status` and `login status`; fallback to file parse. |
| Timeout cancels mid-yield, caller leaks resources | M | M | `finally` cleanup (stderr task + proc.wait); document caller responsibility for workspace cleanup. |
| Probe blocks event loop | L | M | All probes async; subprocess with explicit timeout ≤ 3s. |

## Security Considerations
- Subprocess args passed as a list — no shell, no quoting bugs, no injection from prompt.
- `--sandbox read-only` is the default for chat-completions (phase 03); `workspace-write` only for jobs (phase 05) where the workspace is freshly created and ephemeral.
- `--ask-for-approval never` is required for non-interactive — paired with sandbox enforcement (researcher-01 §9). NEVER pair with `danger-full-access` (anti-pattern, brainstorm §10).
- stderr capped at 64 KiB and only logged at debug level + last 4 KiB included in synthesized error event; structlog redactor (phase 0) catches any leaked tokens.
- Workspace mode `0o700` — only the gateway/worker process user can read.
- `~/.codex` mounted RO — container cannot mutate session token; session refresh stays on host.
- `validate_path_inside` rejects symlinks anywhere in the path chain (defense-in-depth even though sandbox should already block writes outside cwd).
- `app.state.codex_session_healthy` default-deny on startup (False until first probe succeeds) — fail-closed.

## Next Steps
- Phase 03 (chat-completions) consumes `run_codex` with `allow_write=False` and maps `ItemCompleted(AgentMessageItem)` → SSE deltas.
- Phase 04 (responses API) consumes the same iterator and maps to the richer event taxonomy (researcher-02 part B).
- Phase 05 (jobs/Arq) consumes with `allow_write=True` after cloning a public repo into the workspace.
- Phase 08 (hardening) adds: cgroup memory cap, jitter to poller interval, audit log of every spawn (cmd hash + duration).
