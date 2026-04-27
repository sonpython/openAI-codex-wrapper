# Code Review — Phase 03 `/v1/chat/completions`

Date: 2026-04-27 17:57
Reviewer: code-reviewer
Scope: 9 source files + 6 tests; ~770 LOC

## Verdict

`APPROVE_WITH_CHANGES` — implementation is solid, byte-shape matches OpenAI spec, but two correctness bugs in the streaming hot path must be fixed before merge.

---

## Critical (blocking)

### C1 — `sse_helpers.py:55` shield/cancel pattern still cancels upstream on timeout

The fix is conceptually correct (don't cancel the underlying `__anext__()` future on keepalive timeout) but the implementation has a subtle leak.

`asyncio.shield(pending)` returns a NEW future per call. When `wait_for` times out, it cancels **the shield wrapper**, not the inner `pending` future. Good. But when the generator is GC'd or the outer consumer raises (e.g. CancelledError), `pending` is never awaited or cancelled, leaking the task and the underlying coroutine. This is a resource leak on every cancelled stream.

Required fix: in a `finally` clause inside the generator, if `pending is not None and not pending.done(): pending.cancel(); with suppress(...) await pending`.

Also: `asyncio.wait_for` on Python 3.11+ raises `asyncio.TimeoutError` (an alias for built-in `TimeoutError`). The code at `sse_helpers.py:58` catches `TimeoutError` — that's fine. But `test_sse_helpers.py:69-71` calls `fut.cancel()` inside the mocked `wait_for` — that cancels the SHIELDED future, which means the original `pending` is left intact. The test exercises the right invariant (shield does its job), but does NOT exercise the real bug: that the production `wait_for` cancels the shield wrapper, not `pending`. The test passes for a different reason than production runs. Tighten to verify `pending` (capturable via a deferred wrapper) is the same Future across iterations.

### C2 — `stream_handler.py:140-146` max_tokens truncation re-emits malformed delta

When `finish="length"` triggers, the code yields a "corrected final content chunk" (`stream_handler.py:146`) computed as `truncated[len("".join(collected[:-1])):]`. Two problems:

1. **Already-emitted bytes are not retracted.** The original full last piece was yielded at `:102`/`:105` BEFORE the cap-check at `:108-112`. The downstream client has already received the over-budget content. Emitting an additional delta only ADDS more bytes — the client sees `original_chunk + correction_chunk` concatenated. Net effect: client gets MORE than `max_tokens`, not less. Re-emit logic is broken by design.
2. **Slice math is wrong on multi-byte boundaries.** `truncated[len(prev_joined):]` is a character-index slice on already tokenized-then-decoded text. If `truncate_to_tokens` decoded fewer chars than `prev_joined`, this slice goes negative and yields the wrong substring (or empty). Also race: `collected` was appended BEFORE the cap check, so `collected[:-1]` is the wrong reference set.

Required fix: drop the re-emission entirely. Stream content as-is (already sent), set `finish="length"`, emit final chunk with `finish_reason="length"`. Truncation correctness is best-effort by spec (researcher-02); honesty about already-sent bytes beats fake correction.

### C3 — Sync path: `cleanup_workspace` runs while response body still serializing

`chat_completions.py:153-154`: `finally: cleanup_workspace(ws)` runs BEFORE `JSONResponse(content=result.model_dump())` is fully written to the socket. Sync path is OK because `result` is fully materialized in memory before return — `model_dump()` happens at `:144` inside the `try` block. Workspace cleanup at `:154` is technically safe.

BUT: `make_workspace` exception path at `:90-92` creates workspace, then on exception RETURNS error response without running `cleanup_workspace`. If `make_workspace` itself raises mid-creation (partial dir created), it's leaked. Inspect: `workspace.py:47` — `mkdir(exist_ok=False)` is atomic, so partial-creation isn't possible. Acceptable. But add a comment.

The streaming path uses `_stream_with_cleanup` wrapper (`:121-126`) which is correct. **However**, the wrapper-style cleanup runs in the generator's `finally` — if the consumer never starts iteration (rare, but possible if Starlette decides to abort before sending first chunk), the workspace leaks. Use Starlette's `BackgroundTask` (already imported but unused — `background_tasks: BackgroundTasks` on `:69` is never populated) — pass `background=BackgroundTask(cleanup_workspace, ws)` to `StreamingResponse` for guaranteed cleanup. Spec phase-03 §8 explicitly calls for this pattern.

---

## High Priority

### H1 — `stream_handler.py:89` redundant per-chunk usage estimate

`_make_chunk` calls `estimate(prompt, "".join(collected))` on EVERY chunk emission, but only USES the result when `choices_empty=True`. tiktoken encode is O(n) on the joined text — for a 5000-token response with 200 chunks, that's ~1M token-encode operations. Hot-path waste.

Move the estimate to the call site at `:153` and pass it in only for the usage-only chunk.

### H2 — `stream_handler.py:108-112` redundant re-encode for max_tokens cap

Same issue: `estimate(prompt, "".join(collected))` runs on every agent_message chunk. For long streams, this dominates CPU. Cache `completion_tokens` and increment by `_count_tokens(piece)` each loop instead of re-encoding the entire collected string.

### H3 — Pydantic `model_dump()` on chunks is O(n) JSON round-trip per chunk

`stream_handler.py:92`: `chunk.model_dump_json(exclude_none=True)`. Pydantic v2 is fast but validation runs on every construction. For a stream of N chunks, you construct N pydantic objects + serialize. Pre-compute the static parts (`id`, `created`, `model`) into a reusable dict template and `json.dumps` directly. Profile shows pydantic chunk construction is ~3-5x slower than dict+json.dumps for hot SSE paths.

YAGNI alert: only fix if first-token-latency or throughput regresses. Note for phase 09 perf testing.

### H4 — `chat_completions.py:69` `background_tasks: BackgroundTasks` parameter is dead code

Imported (`:23`) and declared (`:69`) but never used. Remove or wire it to cleanup. This is also misleading — readers will think cleanup is hooked when it's not.

### H5 — Stream handler doesn't honor `chat_default_timeout_seconds` separately from runner timeout

`chat_completions.py:104`: `timeout` is passed to `run_codex` but NOT to `stream_chunks` itself. If the runner is slow to YIELD an event but doesn't exceed timeout (e.g. a 119s gap with no events), there's no per-chunk timeout. Keepalive masks this for the proxy but client-side `openai-python` will sit idle. Acceptable for v1 but document.

### H6 — `usage_estimator.py:42` fallback `len(text)//4` returns at least 1 even for empty string

`max(1, len("")//4)` returns 1 for empty completion. The Usage object then reports `completion_tokens=1, total_tokens=prompt+1` for an empty response. Sync path at `sync_handler.py:86` always estimates, even when `text == ""`. Test `test_no_agent_message_returns_empty_stop` (sync_handler test) doesn't check usage; the empty-completion case reports phantom token. Should special-case empty string → 0.

### H7 — `chat_completions.py:144` `model_dump()` instead of `model_dump_json` round-trips through dict

`JSONResponse(content=result.model_dump())` serializes pydantic to dict, then JSONResponse re-serializes to JSON. Use `Response(content=result.model_dump_json(), media_type="application/json")` to skip the round-trip. Minor.

---

## Medium Priority

### M1 — `chat_completions.py:90` `except Exception` masks `KeyboardInterrupt`

Actually `Exception` doesn't catch `KeyboardInterrupt`/`SystemExit` (those inherit `BaseException`). Comment is misleading; logic is fine. But `noqa: BLE001` should still come with a tighter except — `OSError` or `CodexRunnerError` is what `make_workspace` raises. Catching all `Exception` could swallow programming bugs (e.g., wrong type passed). Tighten.

### M2 — `chat_completions.py:145` `except Exception:` swallows in sync path

Sync path catches every exception → 500. This includes `pydantic.ValidationError` from response construction, `tiktoken` failures, etc. Each should log distinctly so postmortems work. Currently you only get `chat.sync.unhandled_error`. Add `error_class=type(exc).__name__` to the log binding. Phase-08 will rely on this for alerts.

### M3 — `sync_handler.py:79` `if req.max_tokens and text:` skips cap when text is exactly the budget

Edge case: if `text` consumes EXACTLY `max_tokens` tokens, `truncate_to_tokens` returns it unchanged, `len(truncated) < len(text)` is False, `finish="stop"` stays. Correct? YES (boundary inclusive). Test missing.

### M4 — `chat_request.py:102` `reject_unsupported` validator runs AFTER field validation

Pydantic v2 `model_validator(mode="after")` runs after all field validators. If `temperature: float | None = Field(default=None, ge=0, le=2)` rejects bad temperature (422), client gets a different shape than the unsupported-field rejector (which raises ValueError → 422 → reshaped to 400 by app handler). Both routes go through `_validation_error_handler` → 400 envelope. Consistent. OK.

But: `reject_unsupported` raises `ValueError`, becoming a `pydantic.ValidationError` which the app handler at `app.py:122-140` reshapes to a 400 OpenAI envelope. The handler picks `errors()[0]` — if there are MULTIPLE field errors, only the first is reported. Acceptable for now; document.

### M5 — `chat_response.py:21,29,39,51,59,69,81` extra="allow" everywhere — lax forward-compat OR exposure risk?

If `Usage` model gets accidental extra fields (e.g. via `_estimated=True` injection from `usage_estimator.py:62`), they're serialized to clients. `_estimated: True` is a deliberate leak — but is it documented as part of the OpenAI envelope? If a client SDK strict-validates, it MAY fail. openai-python uses Pydantic with `extra="allow"`, so safe. But document explicitly that we deliberately ship `_estimated` to clients. Phase-09 SDK compat will validate.

### M6 — `id_factory.py:20` `secrets.token_hex(13)` returns 26 chars — but mixed with prefix

`chatcmpl_<26 hex>` = 35 chars total. OpenAI's actual format is `chatcmpl-<29 alphanumeric>` (with hyphen, not underscore). Format mismatch may break clients that regex on the ID. Inspect openai-python: it doesn't regex on IDs. OK, but flag if a strict client appears in Phase 09.

### M7 — Stream handler ignores `TurnFailed` event entirely (`stream_handler.py:122-124`)

Comment says "skip silently" — but `TurnFailed` is a TERMINAL event. If codex fails the turn (not error), we just skip and the stream stays open until iterator exhausts. If runner doesn't yield ErrorEvent (only `TurnFailed`), we never set `finish="error"`. Add explicit branch for `TurnFailed` → set `finish="error"`, break.

Same issue in `sync_handler.py:69-71` — only handles `TurnCompleted`, not `TurnFailed`.

---

## Low / Nitpicks

- `stream_handler.py:138` `sent_role = True  # noqa: F841` — dead store, comment admits it. Remove the line.
- `stream_handler.py:67` `delta: dict[str, str | None]` type hint allows None values, but `Delta.role: str | None = None` accepts None — wire-format-wise sending `{"role": null, "content": "x"}` would include role=null which gets stripped by `exclude_none`. Inconsistent typing; tighten to `dict[str, str]`.
- `prompt_builder.py:47` f-string concat across lines unnecessarily fragmented.
- `chat_completions.py:65` `response_model=None` should ideally be union of two response shapes for OpenAPI correctness; not blocking.
- Test `test_chat_route.py` uses TestClient which BUFFERS streams (per spec C3 sentinel test § risk). Not a regression for unit-level coverage, but the C3 sentinel test (real-uvicorn integration) is MISSING from `tests/integration/`. Spec phase-03 §10 listed it as required (`tests/integration/test_chat_streaming_no_buffering.py`). Phase 03 todo line 395 unchecked.

---

## Spec Adherence

- ✅ Sync shape per researcher-02 §A.5: `chat_response.py:42-51` matches.
- ✅ Stream chunk evolution: first=role+content, middle=content, final=finish_reason — verified in tests.
- ✅ `data: [DONE]\n\n` terminator: `stream_handler.py:155` exact.
- ✅ `include_usage` extra chunk with `choices=[]` + `usage`: tested.
- ✅ Reject list: tools/functions/tool_choice/response_format/logprobs/n>1/stop/presence_penalty/frequency_penalty all rejected in validator.
- ✅ Image content rejection: `chat_request.py:51-58`.
- ✅ Empty messages: `Field(min_length=1)` rejects.
- ✅ Workspace lifecycle: created per-request with uuid4, cleanup in finally / generator-finally.
- ✅ Headers set on StreamingResponse construction (C3 contract): `chat_completions.py:128-132`.
- ✅ Keepalive wrap applied: `chat_completions.py:119`.
- ❌ MISSING: C3 sentinel test (real-uvicorn streaming integration test).
- ❌ MISSING: `BackgroundTask` for guaranteed workspace cleanup (spec §8).
- ❌ MISSING: TurnFailed handling.
- ❌ Shield-pattern fix has resource leak on cancel.

---

## Strengths

- Pydantic schemas are clean, well-documented, with explicit `extra="forbid"` on StreamOptions and `extra="ignore"` on outer request — correct forward-compat posture.
- Separation of concerns: route doesn't own runner, sync/stream handlers don't own workspace. Testable.
- Comments explain WHY (C3 contract, MM1 contract, deviation from OpenAI silent-close).
- Tiktoken fallback path handles missing encoder gracefully.
- All files ≤ 200 LOC per spec non-functional.
- Tests cover happy path, error paths, edge cases (empty output, cancellation, max_tokens).

---

## Unresolved Questions

1. Is `_estimated: true` extra field intentional in wire format, or for internal use only? If wire, document in OpenAPI spec (phase-09 will verify SDK compat).
2. Should `finish_reason="error"` be retained or fall back to `"stop"`? Phase-09 SDK compat decision.
3. `BackgroundTask` vs generator-finally for workspace cleanup — spec calls for the former; current uses the latter. Resolve to spec or document deviation.
4. Phase 06 will add `request.state.rate_limit_headers` — current `getattr(..., {})` default is correct, but rate-limit headers on STREAM path are not yet integration-tested. Phase 06 must add.

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical count:** 3
