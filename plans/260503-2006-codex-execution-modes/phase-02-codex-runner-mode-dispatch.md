---
title: "Phase 2 — Codex runner mode dispatch"
status: pending
priority: P1
effort: 2.5h
blocks: [phase-04]
blocked_by: [phase-01]
---

# Phase 2 — Codex runner mode dispatch

## Context Links

- Source brainstorm: `plans/reports/brainstorm-260503-2006-codex-execution-modes.md`
- Runner: `src/codex/runner.py`
- Routes hard-coding `allow_write=False`: `src/gateway/routes/chat_completions.py`, `src/gateway/routes/responses.py`
- Job worker (already mode-aware): `src/workers/job_handlers.py`
- Auth middleware: `src/gateway/middleware/auth.py`

## Overview

Plumb `api_key.mode` from auth middleware → chat / responses / job handlers → `run_codex`. Map mode to codex `--sandbox` flag. Lock `--cd` to ephemeral `/workspaces/{job_id}/` (already enforced via `make_workspace`; verify no regression).

## Key Insights

- `runner.run_codex` currently takes `allow_write: bool` (legacy boolean). We replace with explicit `sandbox_mode: str` ∈ `{"read-only", "workspace-write", "danger-full-access"}`. Boolean is too coarse for three states.
- The job worker's `mode` (read-only / workspace-write) is **a different concept** than `api_keys.mode` (sandbox / vps / local-bridge). Don't conflate. Map at the boundary.
- `local-bridge` mode raises explicit `501 not_implemented` from the route layer — never reaches runner. Documented placeholder until P3.
- `--sandbox=danger-full-access` exact spelling MUST be verified live before commit (codex 0.125 docs hint at it but flags can drift). Step 1 below is a hard gate.

## Requirements

### Functional

- [ ] `run_codex` signature: `sandbox_mode: str` replaces `allow_write: bool`. Validates against `{"read-only","workspace-write","danger-full-access"}`.
- [ ] Auth middleware stores `request.state.codex_mode` from `api_key.mode`.
- [ ] `chat_completions` route maps `request.state.codex_mode` → runner sandbox flag.
- [ ] `responses` route same mapping.
- [ ] `local-bridge` mode in either route → 501 with OpenAI-shaped error: `{"error":{"type":"api_error","code":"not_implemented","message":"local-bridge mode not yet supported"}}`.
- [ ] Job worker (`job_handlers.py`) continues to accept its own per-job `mode` arg (independent of api_key mode). When invoked via API, the resolver maps api_key mode → job mode for backward compat.

### Non-Functional

- [ ] `tests/unit -q` all green after refactor.
- [ ] No file > 200 LOC.
- [ ] Backward compat: `mode=sandbox` is bit-identical to today (same `--sandbox read-only`).

## Architecture

### Mapping table

| `api_keys.mode` | runner `sandbox_mode` | codex flag |
|---|---|---|
| `sandbox` | `read-only` | `--sandbox read-only` |
| `vps` | `danger-full-access` | `--sandbox danger-full-access` |
| `local-bridge` | (n/a — 501 in route) | (n/a) |

### Data flow

```
HTTP request
  └─ AuthMiddleware (resolves api_key)
      └─ request.state.codex_mode = api_key.mode
          └─ route handler
              └─ if local-bridge: return 501
              └─ else: sandbox_mode = MAP[api_key.mode]
                  └─ run_codex(sandbox_mode=...)
                      └─ argv += ["--sandbox", sandbox_mode]
```

### Helper

Single mapper lives in `src/codex/runner.py` to avoid scatter:

```python
_MODE_TO_SANDBOX: dict[str, str] = {
    "sandbox": "read-only",
    "vps": "danger-full-access",
}

def resolve_sandbox_flag(api_key_mode: str) -> str:
    """Map api_keys.mode → codex --sandbox value. Raises ValueError on unmapped."""
    try:
        return _MODE_TO_SANDBOX[api_key_mode]
    except KeyError as e:
        raise ValueError(f"unsupported codex mode: {api_key_mode}") from e
```

## Related Code Files

### Modify

- `src/codex/runner.py` — replace `allow_write` with `sandbox_mode`; add `resolve_sandbox_flag`; validate value.
- `src/gateway/middleware/auth.py` — set `request.state.codex_mode = api_key.mode`.
- `src/gateway/routes/chat_completions.py` — read `request.state.codex_mode`; 501 on `local-bridge`; pass `sandbox_mode`.
- `src/gateway/routes/responses.py` — same.
- `src/workers/job_handlers.py` — adapt callsite (currently constructs `allow_write` from job.mode); pass `sandbox_mode` instead.
- `tests/unit/test_*runner*.py`, `tests/unit/test_chat_route.py`, `tests/unit/test_responses_*` — update fixtures to use `sandbox_mode`.
- `docs/system-architecture.md` — add "Codex execution modes" subsection with mapping table.
- `docs/operations-runbook.md` — add per-mode behavior + risk note (prompt injection in vps mode).

### Do not touch

- ApiKey schema (Phase 1).
- SSE handlers (Phase 3).

## Implementation Steps

1. **Verify codex flag (HARD GATE — do before commits)**
   - SSH `root@192.168.1.120`: `docker exec gateway codex exec --help | grep -i sandbox`
   - Confirm `danger-full-access` is the literal value. If different, document the actual flag in this phase file under "Verification log" and use that everywhere.
2. **`src/codex/runner.py`**
   - Add `_MODE_TO_SANDBOX` dict + `resolve_sandbox_flag()`.
   - Change `run_codex` signature: `sandbox_mode: str` (replaces `allow_write: bool`).
   - Drop the `sandbox = "workspace-write" if allow_write else "read-only"` line; use `sandbox_mode` directly.
   - Validate `sandbox_mode in {"read-only","workspace-write","danger-full-access"}` at top — raise `ValueError` early.
   - Keep `--cd workspace_dir` unchanged (already path-bound).
3. **`src/gateway/middleware/auth.py`** — after line 135 (`request.state.tier = api_key.tier`), add `request.state.codex_mode = api_key.mode`.
4. **`src/gateway/routes/chat_completions.py`**
   - Top of streaming + sync branch: `api_mode = getattr(request.state, "codex_mode", "sandbox")`.
   - If `api_mode == "local-bridge"`: return `_openai_error(501, "local-bridge mode not yet supported", error_type="api_error", code="not_implemented")`.
   - `sandbox_flag = resolve_sandbox_flag(api_mode)`.
   - Replace both `allow_write=False` callsites with `sandbox_mode=sandbox_flag`.
5. **`src/gateway/routes/responses.py`** — same pattern; mirror error helper or inline JSONResponse with OpenAI shape.
6. **`src/workers/job_handlers.py`**
   - Line 134-141 currently does `allow_write = mode == "workspace-write"` then `run_codex(... allow_write=allow_write ...)`.
   - Replace with `sandbox_mode = "workspace-write" if mode == "workspace-write" else "read-only"`. (Worker's job.mode is its own enum — independent of api_keys.mode. Keep that boundary.)
   - Pass `sandbox_mode=sandbox_mode` to runner.
7. **Tests**
   - Update every existing test that constructs `run_codex(... allow_write=...)` to use `sandbox_mode=...`.
   - Add new test in `tests/unit/test_chat_route.py`: when `request.state.codex_mode == "local-bridge"` → 501 returned, runner NOT spawned.
   - Add new test: `request.state.codex_mode == "vps"` → runner argv contains `--sandbox danger-full-access`.
   - Mock-based; no live codex needed for unit layer.
8. **Compile + run**
   - `python -m py_compile src/codex/runner.py src/gateway/middleware/auth.py src/gateway/routes/chat_completions.py src/gateway/routes/responses.py src/workers/job_handlers.py`
   - `pytest tests/unit -q`
9. **Docs sync**
   - `docs/system-architecture.md`: insert mapping table + sequence under existing "Codex runner" section.
   - `docs/operations-runbook.md`: warn that `vps` mode disables codex's internal sandbox; container is the boundary; janitor TTL 1h.

## Verification log (fill at execution time)

- Codex version: `___`
- `codex exec --help | grep sandbox` output: `___`
- Confirmed flag: `___` (expected `danger-full-access`)

## Todo List

- [ ] Verify codex 0.125 `--sandbox danger-full-access` flag on remote
- [ ] Refactor `run_codex` to `sandbox_mode`
- [ ] Add `resolve_sandbox_flag` helper
- [ ] Plumb `request.state.codex_mode` in auth middleware
- [ ] Wire chat_completions route (501 + dispatch)
- [ ] Wire responses route (501 + dispatch)
- [ ] Adapt job worker callsite
- [ ] Update unit tests (mock-based)
- [ ] Update docs/system-architecture.md + operations-runbook.md
- [ ] `pytest tests/unit -q` green
- [ ] Compile-check all modified files

## Success Criteria

- [ ] Unit tests green including new mode-dispatch tests.
- [ ] argv inspection: `mode=sandbox` → `--sandbox read-only`; `mode=vps` → `--sandbox danger-full-access`.
- [ ] `mode=local-bridge` → 501 from both `/v1/chat/completions` and `/v1/responses` without invoking runner.
- [ ] No `allow_write` references left in src/ (grep returns 0).

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Codex flag name drifted in 0.125 | Med | High | Step 1 hard gate — verify live, document actual flag, refactor before commit. |
| Concurrent vps jobs share gateway FS | High | Med | Each job owns `/workspaces/{job_id}/`; janitor TTL 1h; `--cd` locks scope. Acceptable per brainstorm. |
| Prompt injection writes outside workspace | Low | High | Codex `--cd` is the contract; document risk in operations-runbook.md; defer scanner to future. |
| Test fixture churn breaks unrelated tests | Med | Low | Single grep-rewrite from `allow_write=False` → `sandbox_mode="read-only"`; sweep one batch. |
| Job worker boundary confusion (api_key.mode vs job.mode) | Med | Med | Comment + boundary mapper at job entry point; do NOT collapse the two enums. |

## Security Considerations

- `vps` mode disables codex's internal sandbox layer. Container is now the only isolation boundary. Brainstorm accepted this for personal/internal use.
- `--cd` flag confines codex's working dir; trust depends on codex respecting `--cd` — verified in P4 E2E.
- `local-bridge` keys cannot trigger runner — 501 short-circuits at route. Defense in depth.

## Next Steps

- Phase 4 E2E verifies the live flag actually unblocks file writes.
- P3 plan reuses `local-bridge` route stub when implementing the WS bridge.
