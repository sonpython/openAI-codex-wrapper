# Phase 04: Responses API

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§11 — exact OpenAI taxonomy locked)
- OpenAI taxonomy: research/researcher-02-openai-event-taxonomy.md (Part B — 53+ events, dual `event:`+`data:` SSE, sequence_number)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (§1–2 — codex emits `agent_message` only on `item.completed`, no incremental text)
- Phase 02: phase-02-codex-runner.md (subprocess + jsonl_parser supplies normalized Codex events)
- Phase 03: phase-03-chat-completions.md (sync/stream pattern reused)

## Overview
- Priority: high
- Status: pending
- Effort: M
- Description: Implement `POST /v1/responses` with EXACT OpenAI Responses API event taxonomy (researcher-02 §B). Harder than chat-completions because (a) dual `event: <name>` + `data: <json>` SSE format, (b) every event carries monotonic `sequence_number`, (c) full lifecycle: `response.created` → `response.in_progress` → `response.output_item.added` → `response.content_part.added` → `response.output_text.delta`* → `response.output_text.done` → `response.content_part.done` → `response.output_item.done` → `response.completed`, (d) Codex emits `agent_message` only on `item.completed` so we must split text into chunks ourselves to simulate streaming deltas.

## Red Team Resolutions
Addresses: **C3** (BaseHTTPMiddleware/SSE buffering — header injection moves to route layer), **MM1** (SSE keepalive heartbeat to prevent idle-timeout silent stream death), **H4 partial** (sequence-number monotonicity under client cancellation — emit `response.cancelled` cleanly).

See phase-03 for the canonical "ASGI middleware + route-set headers" master pattern; this phase applies the same fix.

## Key Insights
- **Dual-line SSE** (researcher-02 §B.1): every emitted event MUST be `event: <type>\ndata: <json>\n\n`. Chat-completions uses data-only — DO NOT reuse the chat emitter.
- **No `[DONE]` sentinel** (researcher-02 §B.1): close socket after `response.completed` or `error`.
- **Monotonic `sequence_number`** (researcher-02 §B.3.*): starts at 0 on `response.created`, increments per emitted event. Stateful; one counter per request.
- **Codex non-incremental text** (researcher-01 §1, §2): `agent_message` arrives in one shot on `item.completed`. To honor `output_text.delta` semantics we MUST chunk it ourselves (e.g., 40-char windows or whitespace boundaries) — document as "approximation, not token-accurate". This is acceptable; OpenAI never guarantees delta granularity.
- **Reasoning items deferred**: codex `reasoning` items map to `response.reasoning_summary_text.delta/done` per researcher-02 §B.2.4 — implementation is non-trivial (separate content_index, summary_index). v1 logs but does not emit; phase-08 hardening revisits.
- **Reject deferred features hard** at request validation: `tools`, `tool_choice`, `previous_response_id`, `truncation`, `parallel_tool_calls`, `text.format` → 400 with explicit `unsupported_parameter` error.
- **SSE header injection MUST happen at the route layer, NOT via `BaseHTTPMiddleware`.** Starlette's `BaseHTTPMiddleware` buffers `StreamingResponse` / `EventSourceResponse` bodies into memory before forwarding (Starlette issue #1012, FastAPI #5536). Headers like `X-RateLimit-*`, `OpenAI-Organization`, `OpenAI-Processing-Ms` MUST be passed into the `EventSourceResponse(headers=...)` constructor inside the route, reading the snapshot dict from `request.scope["state"]["rate_limit_headers"]` populated by raw-ASGI rate-limit middleware (see phase-06). Same pattern as phase-03; that phase documents the master pattern + sentinel test.
- **SSE keepalive heartbeat** (researcher-02 §B has no documented keepalive cadence; OpenAI emits `: <comment>` ~10s): emit `: keepalive\n\n` (SSE comment) every 15s while no other event has flushed, to defeat Caddy/ALB/NAT idle timeouts during long Codex turns. Use shared `gateway/sse_helpers.py` keepalive helper (introduced in phase-00). Comment lines are ignored by SSE consumers — does NOT advance `sequence_number` and does NOT count as a lifecycle event.
- **Cancellation sequence ordering**: when client disconnects mid-stream, the in-flight `response.in_progress` (e.g., seq=N) may never be followed by `response.completed`. Emit `response.cancelled` with `sequence_number = next_seq` (preserves monotonicity), set `response.status="cancelled"`, then close. If the disconnect is detected only via send-side `ConnectionError`, swallow gracefully — no event can be flushed but emitter state is discarded with the request scope.

## Requirements

### Functional
- `POST /v1/responses` accepts pydantic-validated request matching OpenAI Responses API subset (see Schema below).
- `stream=true`: emits SSE per researcher-02 §B with full lifecycle event sequence; closes connection on `response.completed` / `error`.
- `stream=false`: collects all output, returns single `response` object with `status=completed`, populated `output[]`, populated `usage`.
- Reject unsupported parameters with HTTP 400 + OpenAI-style error body `{"error":{"type":"invalid_request_error","code":"unsupported_parameter","param":"tools","message":"..."}}`.
- All response IDs use prefix `resp_` (16 hex chars) per researcher-02 §B.3.1; item IDs use `item_<hex>`.
- `created_at` is ISO-8601 UTC string (researcher-02 §B.3.1); chat uses unix int — keep difference exact.
- Sequence numbers start at 0, increment by 1 per event emitted to client.

### Non-Functional
- Files ≤ 200 LOC each (gateway/routes/responses.py, gateway/schemas/openai_responses.py, gateway/responses_emitter.py).
- p95 first-event latency < 250ms after request hits handler (subprocess startup-dominated).
- Zero memory leak across requests: emitter state lives only for request lifetime.
- structlog `request_id` propagated into every event log.

## Architecture

```
client ── POST /v1/responses ──► routes/responses.py
                                       │
                                       ▼
                       schemas/openai_responses.py  ◄── reject unsupported params (400)
                                       │
                                       ▼
                       codex.runner.stream_events(prompt, mode=read-only)
                                       │  (yields normalized codex events)
                                       ▼
                          responses_emitter.ResponseEmitter
                          ├─ state: seq_no, response_id, output_index, content_index, item_id
                          ├─ on_codex_event(evt) → yields 0..N OpenAI events
                          └─ chunker: splits agent_message text into ~40-char deltas
                                       │
                          stream=true ──┴── stream=false
                                │              │
                                ▼              ▼
                    EventSourceResponse   collect → final Response object
                   (event: ...\ndata:...)        (status=completed, output[], usage)
```

### Data flow (stream=true)

```
T0  client request validated                                  (handler)
T1  emit response.created (seq=0, status=in_progress)         (emitter)
T2  spawn codex subprocess + workspace                         (runner phase 02)
T3  on first codex thread.started → emit response.in_progress (seq=1)
T4  on codex item.started type=agent_message:
       emit response.output_item.added (seq=2)
       emit response.content_part.added (seq=3, type=output_text)
T5  on codex item.completed type=agent_message:
       chunker splits text into N chunks, for each:
          emit response.output_text.delta (seq=4..3+N)
       emit response.output_text.done (seq=4+N, full text)
       emit response.content_part.done (seq=5+N)
       emit response.output_item.done (seq=6+N)
T6  on codex turn.completed:
       emit response.completed with full response object + usage
T7  close socket (no [DONE])
```

### Failure paths
- Codex emits `error` event → emitter outputs `error` event (OpenAI shape) then closes.
- Subprocess exits non-zero before turn.completed → emit `response.failed` with response.status=failed, then `error` event, close.
- Client disconnect mid-stream → cancel scope kills subprocess (phase 02 runner handles); emitter attempts a final `response.cancelled` event (seq increments cleanly); if send fails (ConnectionError) the event is dropped silently and emitter state is discarded with the request scope.

## Related Code Files

### To create
- `src/gateway/routes/responses.py` (≤ 200 LOC) — endpoint handler, request validation, stream/sync split
- `src/gateway/schemas/openai_responses.py` (≤ 200 LOC) — pydantic request model + response object types
- `src/gateway/responses_emitter.py` (≤ 200 LOC) — sequence-numbered event generator, codex→openai mapping, text chunker
- `tests/unit/test_responses_emitter.py` — golden-file tests for event order, sequence numbers, payload shapes
- `tests/unit/test_responses_schema.py` — rejection of unsupported params
- `tests/integration/test_responses_stream.py` — full SSE roundtrip via TestClient against fake codex runner

### To modify
- `src/gateway/app.py` — register `responses_router`
- `src/codex/runner.py` (phase 02) — confirm event normalization exposes `agent_message.text`, `usage` shape, `error` shape

### To delete
- (none)

## Implementation Steps

1. **Define schemas** (`schemas/openai_responses.py`):
   - `ResponsesRequest`:
     ```python
     class ResponsesRequest(BaseModel):
         model: str
         input: str | list[InputItem]   # InputItem = {role, content[]}
         instructions: str | None = None
         stream: bool = False
         temperature: float | None = Field(None, ge=0, le=2)
         max_output_tokens: int | None = Field(None, gt=0)
         metadata: dict[str, str] | None = None
         # explicit reject:
         tools: Any = Field(None, exclude=True)
         tool_choice: Any = Field(None, exclude=True)
         previous_response_id: Any = Field(None, exclude=True)
         truncation: Any = Field(None, exclude=True)
         parallel_tool_calls: Any = Field(None, exclude=True)
     ```
     Use a `model_validator(mode='before')` that scans raw dict for those keys and raises `HTTPException(400, ...)` with OpenAI error body shape; pydantic `extra='forbid'` is too coarse (would also reject unknown future fields).
   - `ResponseObject`, `OutputItem`, `ContentPart`, `Usage` — match researcher-02 §B.4 exactly.

2. **Implement emitter** (`responses_emitter.py`):
   - Class `ResponseEmitter` holds: `response_id`, `model`, `created_at`, `seq=0`, `output_index=0`, `content_index=0`, `current_item_id`, `accumulated_text=""`, `accumulated_usage=None`, `metadata`.
   - Method `_emit(event_type, payload) -> tuple[str, dict]`: stamps `event_id=evt_<hex>`, `type=event_type`, `sequence_number=self.seq`, returns SSE-ready tuple; `self.seq += 1`.
   - Method `start() -> Iterable[tuple]`: yields `response.created` only.
   - Method `on_codex_event(evt) -> Iterable[tuple]`: dispatches by `evt['type']`:
     - `thread.started` → yield `response.in_progress` once
     - `item.started` type=`agent_message` → assign `current_item_id`, yield `output_item.added` then `content_part.added`
     - `item.completed` type=`agent_message` → call `_chunk_text(evt.item.text)` yielding N `output_text.delta`; then `output_text.done` (with full text); `content_part.done`; `output_item.done`
     - `item.completed` type=`reasoning` → log only (defer emission to phase 08); see Risk
     - `turn.completed` → store `usage`; do NOT emit yet (saved for `response.completed`)
     - `error` → yield `error` event with mapped code (codex `TIMEOUT` → `timeout`, default → `server_error`)
   - Method `finalize() -> Iterable[tuple]`: yields `response.completed` with full response object (status=completed, output items collected, usage from last turn.completed). If no `turn.completed` arrived, emit `response.failed` instead.
   - Method `cancel() -> Iterable[tuple]`: yields `response.cancelled` with `response.status="cancelled"`, current accumulated output (may be partial), `sequence_number = self.seq` (then increment). Idempotent — second call yields nothing. Used on client-disconnect path (see route step 3).
   - Text chunker (`_chunk_text`): split `text` at whitespace boundaries into windows of ~40 chars (configurable `RESPONSES_DELTA_CHARS`). Final empty chunk skipped. Pseudocode:
     ```
     def _chunk_text(text, size=40):
         buf = ""
         for word in text.split(" "):
             if len(buf) + len(word) + 1 > size and buf:
                 yield buf
                 buf = word
             else:
                 buf = (buf + " " + word).strip()
         if buf: yield buf
     ```

3. **Implement route** (`routes/responses.py`):
   - Pseudocode:
     ```python
     @router.post("/v1/responses")
     async def create_response(request: Request, req: ResponsesRequest, user=Depends(auth)):
         emitter = ResponseEmitter(model=req.model, metadata=req.metadata or {})
         prompt = build_prompt(req)   # join instructions + input
         # Read rate-limit header snapshot stashed by phase-06 raw-ASGI middleware.
         rl_headers = request.scope.get("state", {}).get("rate_limit_headers", {})
         if req.stream:
             # CRITICAL: pass headers to EventSourceResponse constructor — NOT via post-call middleware injection.
             # BaseHTTPMiddleware buffers SSE bodies (Starlette #1012 / FastAPI #5536); see phase-03 master pattern.
             return EventSourceResponse(
                 _stream(request, emitter, prompt),
                 headers=rl_headers,
                 ping=15,                      # 15s SSE comment keepalive — see Key Insights
             )
         return await _collect(emitter, prompt, headers=rl_headers)

     async def _stream(request, emitter, prompt):
         try:
             for evt_type, payload in emitter.start():
                 yield {"event": evt_type, "data": json.dumps(payload)}
             async for codex_evt in run_codex(prompt, mode="read-only"):
                 if await request.is_disconnected():
                     # Emit response.cancelled before tearing down (best-effort; may not flush)
                     for evt_type, payload in emitter.cancel():
                         yield {"event": evt_type, "data": json.dumps(payload)}
                     break
                 for evt_type, payload in emitter.on_codex_event(codex_evt):
                     yield {"event": evt_type, "data": json.dumps(payload)}
             for evt_type, payload in emitter.finalize():
                 yield {"event": evt_type, "data": json.dumps(payload)}
         except (ConnectionError, asyncio.CancelledError):
             # Client gone; emitter state discarded with request scope.
             pass
     ```
   - For sync path: drain emitter into in-memory list, return JSONResponse with `headers=rl_headers` and last `response.completed` payload's `response` field as body.
   - **Note on `ping=15`**: sse-starlette emits an SSE comment line (`: <ping>\n\n`) every 15s when idle. This is invisible to consumers and does not advance `sequence_number`. Pin sse-starlette version (phase-00) and verify default ping format does not include `event:` line — if it does, override `ping_message_factory` to emit a bare comment.

4. **`build_prompt(req)`** (in `routes/responses.py`):
   - If `req.input` is str: prompt = `(req.instructions + "\n\n" if req.instructions else "") + req.input`
   - If list: flatten `content[].text` per item, prefixed with role marker (`User: `, `Assistant: `).

5. **SSE library**: use `sse-starlette.EventSourceResponse` (already in deps from phase 03). It auto-formats `event:` + `data:` + `\n\n`. Verify it does NOT inject `[DONE]` (it doesn't by default). Configure `ping=15` for keepalive comments. Pin version in phase-00 deps.

6. **Tests** (`test_responses_emitter.py`):
   - Feed canned codex event sequence (from researcher-01 §2 examples) → assert exact emitter output: event types, order, sequence_number values, payload schema.
   - Test text chunker: short text (1 chunk), long text (N chunks), text with newlines.
   - Test error mapping: codex error event → `error` event with correct code.
   - Test reject-unsupported: request with `tools=[...]` → 400, error body matches OpenAI shape.

7. **Integration test** (`test_responses_stream.py`):
   - Use httpx + sse-starlette test client; spawn fake codex runner that yields scripted events.
   - Assert raw bytes of stream: `event: response.created\ndata: ...\n\nevent: response.in_progress\n...`.
   - Assert no `[DONE]` line present.
   - **Real-uvicorn sentinel test** (cf. phase-03 master pattern): spawn the gateway via uvicorn (no TestClient) behind Caddy with `flush_interval -1`; assert first SSE byte arrives at the wire within 1s of subprocess first event. Without this gate, BaseHTTPMiddleware buffering regressions pass CI but break prod (Starlette #1012).
   - **Keepalive sentinel test**: scripted runner stalls 30s before first agent_message; assert at least one `: ` (SSE comment) line emitted within the 30s window so client doesn't see idle gap.
   - **Cancellation sequence test**: client closes connection mid-stream; assert emitter state is discarded cleanly (no thrown exception in server log beyond expected `ClientDisconnect`); next request from same key starts fresh seq=0.

8. **Wire into app**: `app.py` adds `app.include_router(responses_router)`.

## Todo List
- [ ] `schemas/openai_responses.py` with reject-on-unsupported validator
- [ ] `responses_emitter.py` with sequence_number state machine + `cancel()` method
- [ ] Text chunker with whitespace-boundary windows
- [ ] `routes/responses.py` stream + sync paths; **headers passed to `EventSourceResponse(headers=...)` from route, NOT injected by middleware**
- [ ] SSE keepalive: `ping=15` on `EventSourceResponse`; verify shared `gateway/sse_helpers.py` keepalive works under sse-starlette
- [ ] `request.is_disconnected()` polling in stream loop → emitter.cancel() → break
- [ ] `build_prompt()` helper for str/list input
- [ ] Register router in `app.py`
- [ ] Unit tests for emitter (golden sequences)
- [ ] Unit tests for schema rejection
- [ ] Integration test for full SSE stream
- [ ] Real-uvicorn + Caddy sentinel test (first byte < 1s)
- [ ] Keepalive sentinel test (30s idle → ≥1 comment line emitted)
- [ ] Cancellation sequence test (client-disconnect → no server crash; next request seq=0)
- [ ] Document text-delta-is-approximation in docs/system-architecture.md

## Success Criteria
- OpenAI Python SDK `client.responses.create(model="...", input="...", stream=True)` iterates events without errors; events received in order, sequence numbers 0..N strictly monotonic.
- `client.responses.create(stream=False)` returns a `Response` object with `status="completed"`, `output[0].content[0].text` non-empty, `usage.total_tokens > 0`.
- Request with `tools=[...]` returns HTTP 400 with body `{"error":{"type":"invalid_request_error","code":"unsupported_parameter","param":"tools",...}}`.
- All emitter unit tests pass; integration test asserts presence of `event:` line on every emitted event.
- No file in scope exceeds 200 LOC.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Text chunker produces visibly choppy deltas vs OpenAI's token-level | High | Low | Document in API docs; chunker size env-tunable; OpenAI clients don't assert delta granularity |
| Reasoning item emission deferred — may trip o-series users | Med | Med | Document v1 limitation in `/v1/models` metadata; phase 08 adds full reasoning emission |
| Sequence-number off-by-one breaks SDK ordering | Med | High | Unit tests assert exact sequence values; emitter increments only after successful yield |
| `EventSourceResponse` injects `[DONE]` accidentally | Low | High | Integration test asserts no `[DONE]` byte sequence in raw output |
| Codex emits unexpected event type → emitter throws | Med | Med | Unknown event types log warning + skip (not raise); fallback `error` event only on subprocess crash |
| Client disconnect leaks subprocess | Med | High | EventSourceResponse cancellation propagates via anyio cancel scope; runner (phase 02) catches CancelledError → SIGTERM |
| BaseHTTPMiddleware buffers SSE body → first-token p95 < 2s missed in prod | Med | Critical | Headers set in route via `EventSourceResponse(headers=...)`; NO middleware-layer header injection on streaming path; real-uvicorn+Caddy sentinel test gates regressions (Starlette #1012) |
| Long Codex turn idle > 60s → Caddy/ALB/NAT silently kills stream | High | High | `ping=15` keepalive comments via sse-starlette; phase-10 Caddy idle timeout 1h matches |
| Sequence number gap on cancellation confuses SDK | Low | Med | `emitter.cancel()` emits `response.cancelled` with next monotonic seq; if send fails, seq simply stops (SDK-tolerant — connection close ends iteration) |

## Security Considerations
- Reject `metadata` keys/values not matching `^[a-zA-Z0-9_-]{1,64}$` and value len ≤ 512 (OpenAI's published rule) to prevent log injection via metadata.
- structlog redactor (phase 00) auto-scrubs any `authorization`-like keys present in `metadata`.
- `instructions` field length capped at 32k chars before sending to codex; reject 422 if larger (codex CLI has its own limit but we want a clear error first).
- Workspace mode forced to `read-only` for `/v1/responses` (no file writes — this is a chat-style endpoint); only `/v1/codex/jobs` (phase 05) gets `workspace-write`.

## Next Steps
- Phase 05 (`/v1/codex/jobs`) reuses Codex runner + adds Arq queue + git clone + diff capture.
- Phase 08 hardening: implement `response.reasoning_summary_text.*` emission for reasoning items; tighten text chunker token-accuracy via tiktoken.
- Phase 09 SDK compat tests must include OpenAI Python SDK Responses streaming roundtrip.
