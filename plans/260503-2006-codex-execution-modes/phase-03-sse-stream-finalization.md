---
title: "Phase 3 — SSE stream finalization (TransferEncodingError fix)"
status: pending
priority: P1
effort: 1.5h
blocks: [phase-04]
blocked_by: []
---

# Phase 3 — SSE stream finalization

## Context Links

- Source brainstorm: `plans/reports/brainstorm-260503-2006-codex-execution-modes.md` (§ "SSE finalization")
- Chat stream handler (already emits `finish_reason="error"` + `[DONE]`): `src/chat/stream_handler.py`
- Responses stream handler: `src/responses/stream_handler.py`
- Chat route streaming wrapper: `src/gateway/routes/chat_completions.py:_stream_with_usage_capture`
- Responses route streaming wrapper: `src/gateway/routes/responses.py:_stream_with_usage_capture`
- Existing tests: `tests/unit/test_stream_handler.py`, `tests/unit/test_responses_stream_handler.py`

## Overview

`stream_chunks` already finalizes correctly (`finish_reason="error"` then `[DONE]`). The TransferEncodingError on aiohttp clients comes from the **route-level wrapper**: when an exception fires inside `_stream_with_usage_capture` (or the runner raises before any chunk), Starlette tears down the chunked transfer mid-frame. Fix: wrap the route-level generator with try/except that always flushes a synthetic terminal chunk + `[DONE]` (chat) or `response.failed` (responses) before exit.

Belt-and-braces: also harden `stream_chunks` so any unhandled exception inside the loop produces the same final frames the existing `except Exception` branch already emits — current behavior is correct, just add explicit test coverage of codex exit-1 path.

## Key Insights

- `stream_chunks`'s existing `except Exception` branch IS the fix at the handler layer — confirmed by reading lines 174-189. Its terminal frame + `[DONE]` is unconditional via the `finally`-style fall-through.
- The leak is in `_stream_with_usage_capture`: if `kept` raises, exit happens before terminal frame. We need to catch upstream exception, yield terminal SSE bytes, then return — never re-raise.
- Same pattern applies to responses route — its `_stream_with_usage_capture` is structurally identical.
- `keepalive_wrap` itself is safe (already drains pending future on cancel via `finally`). No change needed there.

## Requirements

### Functional

- [ ] Chat route `_stream_with_usage_capture`: on any exception from `kept`, emit a synthetic final chunk (`finish_reason="error"`, empty delta, choices=[]) + usage-only chunk if `include_usage` requested + `data: [DONE]\n\n`, then `return` (do NOT re-raise — Starlette must see clean EOF).
- [ ] Responses route `_stream_with_usage_capture`: on any exception from `kept`, emit `event: response.failed\ndata: {...}\n\n` then `return`.
- [ ] Both wrappers still write `request.state.usage` on the success path.
- [ ] `stream_chunks` keeps current finalization (no behavior change).
- [ ] `stream_responses` keeps current `emitter.finalize()` on exception path (no behavior change).

### Non-Functional

- [ ] No new dependencies.
- [ ] No file > 200 LOC.
- [ ] New test simulates codex exit-1 and asserts client byte stream contains `finish_reason="error"` AND `[DONE]` markers — and the generator exhausts cleanly (no leaked exception).

## Architecture

### Failure modes covered

| Source of failure | Today | After fix |
|---|---|---|
| Codex exits non-zero, runner yields ErrorEvent | OK (handler emits error+[DONE]) | OK (no change) |
| Codex exits non-zero, runner yields nothing | OK (`stream_chunks` `not sent_role` branch) | OK (no change) |
| Runner raises during `async for raw in proc.stdout` | unwinds through `keepalive_wrap` → `_stream_with_usage_capture` → Starlette mid-frame → aiohttp TransferEncodingError | wrapper catches, flushes terminal frames, returns |
| Token estimator raises in `_count_tokens` | mid-frame TransferEncodingError | wrapper catches, flushes terminal frames |
| Workspace cleanup (BackgroundTask) raises | runs after body — irrelevant to SSE close | unchanged |

### Wrapper template (chat)

```python
async def _stream_with_usage_capture() -> AsyncIterator[bytes]:
    completion_bytes = 0
    sent_done = False
    try:
        async for chunk in kept:
            if chunk.startswith(b"data: ") and not chunk.startswith(b"data: [DONE]"):
                completion_bytes += len(chunk)
            if chunk.startswith(b"data: [DONE]"):
                sent_done = True
            yield chunk
    except Exception:
        logger.exception("chat.stream.wrapper_failed", job_id=job_id)
        if not sent_done:
            # Emit synthetic terminal frame + [DONE] so aiohttp clients see a
            # clean chunked-transfer EOF instead of TransferEncodingError 400.
            yield _synth_error_chunk(req.model, cid, created)
            yield b"data: [DONE]\n\n"
            sent_done = True
        # do NOT re-raise — let StreamingResponse close cleanly
    finally:
        prompt_tokens = _count_tokens(prompt)
        completion_tokens = max(1, completion_bytes // 4)
        request.state.usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
```

`_synth_error_chunk` is a small helper (≤15 LOC) added next to the wrapper or in a new `src/chat/error_chunk.py` if route file approaches 200 LOC.

### Wrapper template (responses)

```python
async def _stream_with_usage_capture() -> AsyncIterator[bytes]:
    completion_bytes = 0
    sent_terminal = False
    try:
        async for chunk in kept:
            if not chunk.startswith(b": keepalive"):
                completion_bytes += len(chunk)
            if chunk.startswith(b"event: response.completed") or chunk.startswith(b"event: response.failed"):
                sent_terminal = True
            yield chunk
    except Exception:
        logger.exception("responses.stream.wrapper_failed", response_id=response_id)
        if not sent_terminal:
            yield _sse_bytes_failed(response_id)  # synthetic response.failed
        # do NOT re-raise
    finally:
        # write usage as before
```

## Related Code Files

### Modify

- `src/gateway/routes/chat_completions.py` — wrap `_stream_with_usage_capture` with try/except/finally.
- `src/gateway/routes/responses.py` — same.
- `tests/unit/test_chat_route.py` (or new `tests/unit/test_chat_stream_finalization.py`) — codex exit-1 simulation asserting both error chunk AND `[DONE]` present.
- `tests/unit/test_responses_route.py` (or equivalent) — same for responses.

### Optional create (only if route files cross 200 LOC after edits)

- `src/chat/error_chunk.py` — synthetic terminal-chunk helper.
- `src/responses/error_event.py` — synthetic `response.failed` helper.

### Do not touch

- `src/chat/stream_handler.py` — already correct.
- `src/responses/stream_handler.py` — already correct.
- `src/gateway/sse_helpers.py` — already correct.

## Implementation Steps

1. **Audit**: confirm `stream_chunks` final-error path (lines 174-189) still emits both terminal chunk and `[DONE]` — read once before editing the route.
2. **Chat route**:
   - Add `_synth_error_chunk(model, cid, created)` helper near top of file (or `src/chat/error_chunk.py` if size).
   - Replace existing `_stream_with_usage_capture` with the try/except/finally pattern. Track `sent_done` flag.
   - On exception: log via structlog `exception` (preserves traceback), yield error chunk + `[DONE]`, set flag, never re-raise.
3. **Responses route**:
   - Add `_synth_failed_event(response_id)` helper that returns `event: response.failed\ndata: {"type":"response.failed","response":{"id":...,"status":"failed"}}\n\n` bytes.
   - Same wrapper rewrite.
4. **Tests**
   - **Chat**: build a fake `events: AsyncIterator[CodexEvent]` that raises `RuntimeError("codex_died")` after first `ItemCompleted`. Pass through `stream_chunks` → `keepalive_wrap` → `_stream_with_usage_capture`. Assert collected bytes include `b"finish_reason\":\"error\""` AND `b"data: [DONE]\\n\\n"`. Assert no exception propagates out.
   - **Responses**: same shape but assert `event: response.failed` line present.
   - **Regression**: success-path test — codex completes normally, output ends with `data: [DONE]\n\n` exactly once (no double-emit due to wrapper).
5. **Manual verify with curl** (deferred to P4 deploy step):
   - Force a `vps` key with a prompt designed to make codex exit-1 (e.g. malformed config). Confirm response body has `[DONE]` and curl doesn't error.
6. **Compile + run**
   - `python -m py_compile src/gateway/routes/chat_completions.py src/gateway/routes/responses.py`
   - `pytest tests/unit -q -k stream`

## Todo List

- [ ] Read existing finalization to confirm no double-emit
- [ ] Add `_synth_error_chunk` helper (chat)
- [ ] Add `_synth_failed_event` helper (responses)
- [ ] Rewrap chat `_stream_with_usage_capture`
- [ ] Rewrap responses `_stream_with_usage_capture`
- [ ] Test: chat error path bytes contain `finish_reason="error"` + `[DONE]`
- [ ] Test: chat success path emits `[DONE]` exactly once
- [ ] Test: responses error path bytes contain `response.failed`
- [ ] Test: responses success path no double terminal event
- [ ] `pytest tests/unit -q` green
- [ ] Compile-check both route files

## Success Criteria

- [ ] New unit test simulating codex exit-1 passes — body contains both error frame and `[DONE]` (or `response.failed`).
- [ ] `request.state.usage` still populated on success path (verified by existing tests).
- [ ] No regression in `tests/unit/test_stream_handler.py` or `tests/unit/test_responses_stream_handler.py`.
- [ ] Manual smoke (P4): Open WebUI streaming on forced-error prompt does NOT raise TransferEncodingError client-side.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Double-emit `[DONE]` in success path | Med | Med | `sent_done` flag guards against duplicate; tested. |
| Swallowing exception hides genuine bug from observability | High | Low | `logger.exception` keeps traceback; metric `codex_subprocess_exit_code{code="nonzero"}` already covers signal. |
| Synthetic chunk mismatches OpenAI shape (clients reject) | Low | Med | Reuse `ChatCompletionChunk` model with empty choices + `finish_reason="error"` — same shape `stream_chunks` already emits. |
| `request.state.usage` write fires after error → middleware can't true-up | Low | Low | Acceptable; partial usage = minimum 1 token. Documented behavior. |
| BackgroundTask cleanup leaks workspace if error before stream begins | Low | Low | Already covered by `BackgroundTask(cleanup_workspace, ws)` — runs regardless of generator outcome. |

## Security Considerations

- Synthetic error chunk MUST NOT include `stderr_tail` or any internal trace — only `finish_reason="error"` and empty delta. (PII / leakage hygiene.)
- Logger uses `exception` so internal trace is captured server-side, not in response body.

## Next Steps

- Phase 4 E2E forces an error path against a real codex process (e.g., `vps` mode with a deliberately malformed prompt) to confirm aiohttp clients no longer raise.
