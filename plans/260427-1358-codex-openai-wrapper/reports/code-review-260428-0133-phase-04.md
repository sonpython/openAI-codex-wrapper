# Phase 04 — Code Review: `/v1/responses` (OpenAI Responses API)

Reviewer: code-reviewer (Staff)  Date: 2026-04-28  Scope: 7 src files (985 LOC) + 6 test files

## Verdict
**APPROVE_WITH_CHANGES** — Core taxonomy, sequence numbers, dual-line SSE format, BackgroundTask cleanup, and keepalive integration are all correct. **Three real bugs** will surface against the OpenAI SDK and one will produce a server crash on multi-message turns. None block merge if hot-fixed before SDK smoke tests in phase-09.

Critical: 1 · High: 4 · Medium: 4 · Low/Nit: 5

---

## Critical (must fix before SDK smoke)

**C1. Multiple `agent_message` items will collide on `output_index=0`** — `src/responses/events_emitter.py:107-139`, `src/responses/responses_helpers.py:84-114`. Every `output_item.added`/`content_part.added`/`delta`/`done`/`output_item.done` payload hardcodes `"output_index": 0` and `"content_index": 0`. Codex can emit multiple `agent_message` items per turn (review-time fixture in `test_responses_sync_handler.py:208-227` confirms it). Each new message is a new output item per OpenAI taxonomy (researcher-02 §B.2.2). Result against SDK: SDK keys text buffers by `(output_index, content_index)` and **silently overwrites** prior messages — only the last item's text survives reassembly. Fix: stateful `_output_index` counter on the emitter, incremented in `ItemStarted` handler; pass into `emit_agent_message_events`. The `_current_item_id` logic also blindly overwrites a prior item's id without emitting `output_item.done` for it — the prior item never gets a terminal event.

---

## High

**H1. `text` field (with `text.format`) is NOT rejected** — `src/gateway/schemas/responses_request.py:19-26`. Spec calls out `text.format` in deferred list (phase-04 spec line 27, 134; this file's own docstring line 8). Reject-list omits `text`. Sending `{"text": {"format": "json"}}` falls through to `extra="ignore"` and is silently accepted. This is in the explicit Success Criteria and visible in Test 99 parametrize — **the test does not cover `text`**. Fix: add `"text": "text.format is not supported in v1"` to `_REJECTED_FIELDS` and add a parametrize row.

**H2. Empty input not rejected** — `src/gateway/schemas/responses_request.py:73-90`, `src/responses/responses_helpers.py:117-156`. `input: ""` (string) and `input: []` (empty list) both pass validation; `build_responses_prompt` then synthesizes a prompt that contains only `"User:\n\n\nAssistant:\n"` (or just `"\n\nAssistant:\n"` for empty list with no instructions). OpenAI rejects empty `input` with 400 invalid_request. Fix: `Field(min_length=1)` on `input` (covers str via `min_length`; for list use `model_validator` checking len > 0 and that each item.content non-empty).

**H3. Rejected-field error returns wrong `code` in production** — `src/gateway/routes/responses.py` (uses no custom validation handler) and `src/gateway/app.py:123-141`. The validator at `responses_request.py:104` raises `ValueError("unsupported_parameter:tools:…")` which pydantic wraps into a `RequestValidationError`. The **production** handler at `app.py:127-141` extracts `msg=first.get("msg")` and unconditionally sets `"code": "invalid_request_error"` — it does **not** parse the `unsupported_parameter:` prefix. So real clients see `code=invalid_request_error` not `code=unsupported_parameter`, and `param=null`. The test app at `test_responses_route.py:44-68` parses the prefix correctly — so tests pass while prod is wrong. Fix: move the prefix-parsing branch from the test fixture into the real `_validation_error_handler` in `app.py`, OR catch in the route via a custom dependency before pydantic wraps. Currently this defeats the purpose of the structured prefix.

**H4. `text.split(" ")` chunker collapses runs of whitespace** — `src/responses/responses_helpers.py:23-39`. Input `"a  b\n\nc"` → splits on single space only, embedded `\n` and double-spaces survive into chunks but the assembled `text` field on `output_text.done` (line 92) is the **original** `full_text`. So the SDK reassembles deltas joined naively and gets `"a   b\n\nc"` while server's `done.text` says `"a  b\n\nc"` — mismatch. OpenAI clients usually trust `done.text`, so functional impact is low, but byte-level diff tests will fail. Fix: `text.split()` (whitespace-greedy) and join with `" "`, or stream raw windows of `size` characters without word-splitting.

---

## Medium

**M1. `events_emitter.py` exceeds 200-LOC cap (205)** — Real overage by 5 lines. Cohesion is fine. Cheap fix: collapse the duplicated `_snapshot(...)` calls in `finalize`/`cancel` (lines 182-205) by extracting `_terminal_snapshot(status)` helper, or move `_snapshot` into `responses_helpers.py`. Drops to ~190 LOC trivially.

**M2. `cancel()` does not flush partial in-progress text** — `src/responses/events_emitter.py:197-205`. If client disconnects mid-turn after `output_item.added` + `content_part.added` but before `item.completed`, the cancel path emits `response.cancelled` with `output=self._output_items` (empty list — items only appended on `item.completed`). The opened `output_item` and `content_part` never get their `.done` events. SDK may strict-check that every `.added` has matching `.done` (researcher-02 §B.2.2 implies it). Mitigation either (a) emit `output_item.done` with `status="incomplete"` before cancelled, or (b) document partial state is intentional. Phase-04 spec line 30 (Key Insights) actually documents this is acceptable — but no test asserts the behavior either way. Add a unit test pinning current behavior so SDK regressions surface clearly.

**M3. Sync-path item id is deterministic and predictable** — `src/responses/sync_handler.py:126`: `item_id = f"item_{response_id[5:]}"`. This makes item id leak the response id hex. Streaming path generates fresh `item_<10-byte-hex>` ids via `new_item_id()`. Inconsistent and exposes id correlation. Use `new_item_id()` for parity.

**M4. `OutputTokensDetails` default uses class-level mutable default via `= OutputTokensDetails()`** — `src/gateway/schemas/responses_object.py:29`. Pydantic v2 deep-copies model defaults so it's safe in practice, but it's a footgun for mypy and makes `ResponseUsage` instances share a sentinel reference at the type-checker level. Prefer `default_factory=OutputTokensDetails`. Same pattern in `OutputItem.content: list[...] = []` (line 60) — pydantic copies, but mutable default is conventionally a `default_factory=list`.

---

## Low / Nitpicks

**L1. `_make_response_id` and `_iso_now` duplicated** between `routes/responses.py:56-62` and `responses_helpers.py:42-54` (`new_item_id`, `iso_now`). Move `_make_response_id` into `responses_helpers.py` (next to `new_item_id`/`new_event_id`) and import. Also `iso_now` is never imported in routes.py — uses `_iso_now` redefinition instead.

**L2. `noqa: BLE001` plus `Exception` swallow on workspace creation** — `responses.py:93-95`. Same pattern as chat-completions phase-03 so consistency-wise OK; but `make_workspace` is documented to raise `CodexRunnerError` only — narrow the catch.

**L3. `responses_helpers.build_responses_prompt` raises `ValueError` for context-too-long** — caller maps to HTTP 400 with `code="context_length_exceeded"` (responses.py:86). OpenAI's documented code for this is `context_length_exceeded` with `type=invalid_request_error` — current code is correct.

**L4. `_output_items` mutated via parameter passing** — `responses_helpers.emit_agent_message_events` mutates a list owned by the emitter (helper module touching emitter state via list reference). Fragile; once C1 forces a stateful index counter, fold this back into the emitter as a method.

**L5. `noqa: SIM105` "contextlib.suppress cannot wrap yield"** — `stream_handler.py:89, 102`. The pattern `try: yield ... except Exception: pass` inside a generator's `except` block is correct; the comment is fine. Minor: catching bare `Exception` after the outer `except (CancelledError, GeneratorExit):` could mask OOM. Use `except OSError`.

---

## Spec Adherence (researcher-02 §B vs implementation)

| Item | Status |
|---|---|
| Dual `event:`+`data:` lines, `\n\n` terminator | OK (`stream_handler.py:38`) |
| `event:` line == `payload.type` | OK (test `test_payload_type_matches_event_line`) |
| Lifecycle order created→in_progress→item.added→content_part.added→delta→text.done→content_part.done→item.done→completed | OK (test_golden_event_order_types) |
| `sequence_number` monotonic from 0 | OK |
| No `[DONE]` sentinel | OK (asserted in test) |
| `response.completed` carries full `output[]` + `usage` | OK |
| `id = resp_<26 hex>` | OK (`token_hex(13)` → 26 hex chars; tests assert prefix `resp_`) |
| `created_at` ISO-8601 UTC string | OK |
| `usage.output_tokens_details.reasoning_tokens` | OK |
| Reject `tools/tool_choice/previous_response_id/truncation/parallel_tool_calls/reasoning` | OK |
| Reject `text.format` | **MISSING** (H1) |
| Reject empty input | **MISSING** (H2) |
| C3 (route-layer headers, no BaseHTTPMiddleware) | OK |
| MM1 (`keepalive_wrap` 15s, comment-only) | OK |
| BackgroundTask cleanup on stream + sync | OK (tested) |
| Multiple agent_message → multiple output_items | **BROKEN** (C1) |
| `response.cancelled` emitted on disconnect with monotonic seq | OK |

---

## Strengths

- Sequence-number monotonicity invariant is enforced **and tested** (`test_golden_sequence_numbers_monotonic`, `test_cancel_sequence_number_monotonic`, `test_sequence_numbers_monotonic_across_stream`).
- First-byte sentinel test (`test_first_chunk_starts_with_response_created_prefix`) is exactly the right gate for SDK regressions.
- Keepalive interleaving is asserted to NOT corrupt event:/data: pairing (`test_keepalive_does_not_suppress_real_events`, `test_keepalive_position_between_events`).
- `cancel()` idempotency is tested.
- BackgroundTask tested on both sync and stream paths — phase-03 lessons absorbed.
- `text="" + chunker` returns no deltas (test_chunk_empty_text + downstream skip via `if buf` guard) → no malformed `delta=""` events.
- Auth middleware skip-list does NOT include `/v1/responses` (auth.py:48-62) → endpoint correctly default-denied.

---

## Recommended Actions (in order)

1. **C1** — fix output_index counter (1-2 hr): single hottest bug; emerges first agent_message after a reasoning item.
2. **H3** — fix unsupported_parameter mapping in `app.py` validation handler.
3. **H1** — add `text` to `_REJECTED_FIELDS` + parametrize row.
4. **H2** — `Field(min_length=1)` on input + non-empty list validator.
5. **H4** — chunker uses `str.split()` not `split(" ")`.
6. **M1** — extract `_terminal_snapshot` helper, drop emitter to ≤200 LOC.
7. **M2** — pin behavior: emit `output_item.done(status="incomplete")` before `response.cancelled` OR add explicit "intentionally partial" test + docstring.
8. **M3, M4, L1-L5** — cleanup pass.

## Metrics

- Files/LOC: 7 src (985 total) — 1 over cap (events_emitter.py: 205).
- Test files: 6, ~25 happy + ~15 negative-path tests; sequence/byte/keepalive/cancellation all covered.
- Type coverage: high (pydantic + Literal everywhere); only `dict[str, object]` in stream_handler is loose.
- Lint: `noqa` markers all justified.

## Unresolved Questions

1. Will SDK strict-validate that every `output_item.added` has matching `output_item.done` before `response.cancelled`? If yes, M2 escalates to High.
2. Spec says `id = resp_<26 hex>` (researcher-02 §B.3.1 example shows `resp_123` short form, not 26 hex) — confirm 26 vs 16 hex with real OpenAI traffic in phase-09 smoke.
3. Should `metadata: {}` collapse to `null` in the JSON output (current: `metadata or None` → null)? OpenAI returns `metadata: {}` literally. Cosmetic but visible in SDK round-trip.
4. `_chunk_text` does not yield control between chunks (no `await asyncio.sleep(0)`). For 200KB outputs this could starve the loop briefly. Profile in phase-08.

---

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical count:** 1
