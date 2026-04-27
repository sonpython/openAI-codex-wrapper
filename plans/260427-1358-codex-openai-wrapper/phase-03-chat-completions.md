# Phase 03: Chat Completions

## Context Links
- Brainstorm: `../reports/brainstorm-260427-1358-codex-openai-wrapper.md` (§2 endpoint scope, §6 JSONL→OpenAI mapping, §10 anti-patterns)
- Phase 00: `phase-00-bootstrap.md` (settings, logging, structlog redaction)
- Phase 01: `phase-01-auth-and-models.md` (provides `request.state.user_id` + `request.state.api_key`)
- Phase 02: `phase-02-codex-runner.md` (provides `run_codex`, `make_workspace`, `cleanup_workspace`, event types)
- OpenAI taxonomy: `research/researcher-02-openai-event-taxonomy.md` (Part A — wire format, schemas, error handling, non-streaming baseline)
- Codex JSONL: `research/researcher-01-codex-jsonl-schema.md` (§2 item types, §4 `--ephemeral`, §6 version pin)
- Project rules: `../../.claude/rules/development-rules.md`

## Overview
- Priority: critical
- Status: pending
- Effort: M
- Description: Implement `POST /v1/chat/completions` with two response modes — sync (`stream: false`) returning a `chat.completion` object, and SSE streaming (`stream: true`) emitting `chat.completion.chunk` events terminated by `data: [DONE]`. Text-only by design (locked decision, brainstorm §2). The endpoint consumes the codex runner from phase 02 with `allow_write=False`, joins all `agent_message` text from `ItemCompleted` events, and shapes output to byte-for-byte OpenAI parity per researcher-02 Part A.

## Red Team Resolutions
- **C3 (BaseHTTPMiddleware + StreamingResponse buffering)** — Headers (including `X-RateLimit-*` from phase 06) MUST be set by THE ROUTE HANDLER inside `StreamingResponse(headers=...)` BEFORE return — never injected by middleware after `call_next`. Phase 06's rate-limit middleware will stash limits in `request.state.rate_limit_headers` (dict); route reads + includes in StreamingResponse headers. Sentinel test added: real-uvicorn integration test asserts first SSE byte arrives < 1s of subprocess first event (TestClient is not sufficient — it buffers).
- **C1 (--ephemeral)** — Now gated on phase-02 runner's `settings.CODEX_HAS_EPHEMERAL` flag, which is set by phase-00 `make verify-codex`. Route layer doesn't construct argv directly.
- **MM1 (SSE keepalive)** — `stream_chunks` wrapped with `sse_helpers.keepalive_wrap(..., interval=15.0)` (helper from phase-00). Emits `: keepalive\n\n` SSE comment when codex events idle > 15s. Prevents Caddy/CDN/AWS-ALB idle timeouts from killing slow streams.

## Key Insights
- Per researcher-02 §A.1: chat-completions SSE is **data-only** — no `event:` line. That is different from `/v1/responses` (phase 04). Don't accidentally mix the two formats.
- Per researcher-02 §A.3: stream chunk evolution is strict — first chunk has `delta.role="assistant"` (typically with `content` already starting), middle chunks have `delta.content` only, final chunk has `finish_reason="stop"`. If `stream_options.include_usage=true`, an EXTRA chunk follows with `choices: []` + `usage` populated.
- Per researcher-02 §A.4: OpenAI's mid-stream error behavior is "close the connection silently". We deviate intentionally — emit a final chunk with `finish_reason="error"` THEN `data: [DONE]`, log the codex error. Reason: silent-close is harder to debug; we still let openai-python parse cleanly because `finish_reason` is a documented enum value but not gated to the standard four (`stop|length|tool_calls|content_filter`). Document this deviation.
- Per researcher-01 §4: `--ephemeral` flag means no thread persistence to disk — matches OpenAI chat-completions stateless semantics perfectly. **Use only when `settings.CODEX_HAS_EPHEMERAL` is True** (set by phase-00 `make verify-codex`). The route layer does NOT inspect this flag — phase-02's `run_codex` handles the branching internally so this phase only calls `run_codex(prompt, allow_write=False, ...)`.
- **C3 — SSE headers come from the route, not middleware.** BaseHTTPMiddleware buffers `StreamingResponse` bodies into memory before forwarding (Starlette issue #1012, fastapi #5536). To preserve streaming, ALL response headers (rate-limit headers from phase 06, content-type, cache controls) MUST be set in the route handler when constructing `StreamingResponse(content=..., headers={...})`. Phase 06 rate-limit middleware will populate `request.state.rate_limit_headers: dict[str, str]` BEFORE `call_next`; this route reads that dict and merges into the StreamingResponse headers. Phase 06's UsageTracking middleware (the BaseHTTPMiddleware suspect) is restricted to NON-streaming paths only or moved to ASGI-direct.
- **MM1 — Keepalive heartbeat for slow streams.** Long codex runs may have 30-90s gaps between agent_message events. Without a heartbeat, intermediary proxies (Caddy default 30s idle, CDN, AWS-ALB) kill the connection silently. Wrap the SSE iterator with `sse_helpers.keepalive_wrap(..., interval=15.0)` (helper from phase-00, also used by phases 04/05).
- Token accounting honesty: codex's `turn.completed.usage` is upstream-attributed (input_tokens includes Codex's system prompt + tool scaffolding, not just our prompt). Reporting that to the OpenAI client would be misleading. We'll use **tiktoken** locally on the user-supplied messages + assistant response, override Codex's numbers, and document this as best-effort. Add `usage._estimated: true` (extra field — pydantic chunk model is `extra=allow`).
- Reject unsupported features explicitly with 400 (clearer than silently ignoring): `tools`, `functions`, `tool_choice`, `response_format`, `logprobs`, `n>1`. This matches OpenAI's behavior of returning structured 400 for unknown parameter values, and avoids client confusion when expected behavior differs.
- Sandbox is `read-only`: chat-completions runs in an ephemeral workspace dir but Codex can't write to it. The dir exists only because `--cd` requires a path; nothing is written. Cleanup is still important (empty dir).
- Token budget: this phase is the "happy path" of value. Don't add anything beyond what researcher-02 Part A documents. YAGNI.

## Requirements

### Functional
- `POST /v1/chat/completions` accepts a body validating against `ChatCompletionRequest` (pydantic). Auth required (middleware from phase 01).
- Sync mode (`stream: false` or omitted): returns `200 application/json` with body shape per researcher-02 §A.5.
- Stream mode (`stream: true`): returns `200 text/event-stream` with chunk shapes per researcher-02 §A.2-A.3 + final `data: [DONE]\n\n`. Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`.
- `stream_options.include_usage: true` → extra final chunk with `choices: []` + `usage` (best-effort). Per researcher-02 §A.3 final-with-usage example.
- Reject with HTTP 400 + OpenAI error envelope (phase 01 helper) when request includes any of: `tools`, `functions`, `tool_choice`, `response_format`, `logprobs=true`, `n` other than 1, `stream` and image content (skip vision in v1).
- `model` field required but ignored beyond echoing back; only `codex-cli` officially supported, but accept any string and echo (clients sometimes send `gpt-4o-mini` unconditionally — be lenient).
- Prompt construction: `build_prompt(messages)` joins the role-prefixed blocks (PDF skeleton). Handles `system`, `user`, `assistant` roles. Vision parts (image_url content blocks) → 400. Multi-turn dialogue → flattens with role prefixes.
- Workspace lifecycle: `make_workspace(uuid4())` per request → run → cleanup in `finally`.
- Token usage: tiktoken-counted prompt + response chars on `cl100k_base` encoding (tiktoken is fine on free-tier; no network call).
- Connection cancel (client disconnect): propagate cancellation to `run_codex` (phase 02 handles SIGTERM); cleanup workspace; log `chat.client_disconnect`.

### Non-Functional
- Each Python file ≤ 200 LOC. Anticipated split: `routes/chat.py` (router), `chat/request_schema.py` (pydantic), `chat/prompt_builder.py`, `chat/sync_response.py`, `chat/stream_response.py`, `chat/usage_estimator.py`.
- p95 first-token latency < 2s on a warm stream (success metric, brainstorm §9).
- SSE writes flushed promptly — use `StreamingResponse` with `media_type="text/event-stream"` and `await asyncio.sleep(0)` or rely on uvicorn's auto-flush per yield.
- Total output capped at `max_tokens` if specified — we honor it as a soft cap by stopping iteration once tiktoken-counted output exceeds it (best-effort; codex doesn't accept a max-tokens flag).
- ruff + mypy clean; pydantic v2 strict mode where useful (`extra="forbid"` on request schema for clear 400s).

## Architecture

```
client (OpenAI SDK)
   │
   ▼
POST /v1/chat/completions
   │ AuthMiddleware (phase 01) → request.state.user_id
   ▼
┌──────────────────────────────────────────────────┐
│ chat router                                      │
│                                                  │
│  1. validate ChatCompletionRequest               │
│  2. reject unsupported (tools/n>1/etc) → 400     │
│  3. prompt = build_prompt(messages)              │
│  4. ws = make_workspace(uuid4())                 │
│  5. iter = run_codex(prompt, allow_write=False,  │
│       workspace_dir=ws, timeout=settings...)     │
│  6. if stream: return StreamingResponse(...)     │
│     else:      collect → return ChatCompletion   │
│  7. finally: cleanup_workspace(ws)               │
└──────────────────────────────────────────────────┘
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
sync collector                  stream emitter
   │                                   │
   for evt in iter:                    for evt in iter:
     if AgentMessageItem:                if AgentMessageItem:
       parts.append(evt.text)              yield delta_chunk(evt.text)
     if ErrorEvent:                      if ErrorEvent:
       raise                               yield finish(error) + DONE
     if TurnCompleted:                   if TurnCompleted:
       break                               yield finish(stop) + usage? + DONE
   return ChatCompletion(...)          (writes already streamed)
```

Sync response shape (researcher-02 §A.5):
```json
{"id":"chatcmpl-...", "object":"chat.completion", "created":<ts>, "model":"codex-cli",
 "choices":[{"index":0,"message":{"role":"assistant","content":"..."},"finish_reason":"stop","logprobs":null}],
 "usage":{"prompt_tokens":N,"completion_tokens":M,"total_tokens":N+M}}
```

Stream chunk evolution (researcher-02 §A.3):
```
data: {first chunk: delta={"role":"assistant","content":"first piece"}, finish_reason:null}\n\n
data: {middle chunk: delta={"content":"more"}, finish_reason:null}\n\n
data: {final chunk: delta={}, finish_reason:"stop"}\n\n
[if include_usage:] data: {choices:[], usage:{...}}\n\n
data: [DONE]\n\n
```

## Related Code Files

### To create
- `src/gateway/routes/chat.py` (router; ≤ 100 LOC)
- `src/gateway/schemas/chat_request.py` (pydantic ChatCompletionRequest + Message + ContentPart; ≤ 200 LOC)
- `src/gateway/schemas/chat_response.py` (ChatCompletion + ChatCompletionChunk + Choice + Delta + Usage; ≤ 150 LOC)
- `src/chat/__init__.py`
- `src/chat/prompt_builder.py` (`build_prompt(messages) -> str`; ≤ 80 LOC)
- `src/chat/sync_handler.py` (sync collector path; ≤ 150 LOC)
- `src/chat/stream_handler.py` (SSE emitter path; ≤ 200 LOC)
- `src/chat/usage_estimator.py` (tiktoken wrapper + estimator; ≤ 80 LOC)
- `src/chat/id_factory.py` (`new_completion_id()` → `chatcmpl_<26 b32 chars>`; ≤ 30 LOC)
- `tests/unit/test_prompt_builder.py`
- `tests/unit/test_chat_request_validation.py`
- `tests/unit/test_sync_handler.py` (with mocked `run_codex` async generator)
- `tests/unit/test_stream_handler.py` (with mocked iterator; assert exact SSE byte sequence)
- `tests/integration/test_chat_completions_e2e.py` (real codex; skipped without auth)
- `tests/compat/test_openai_python_sdk.py` (uses `openai.OpenAI(base_url=...)` — skipped if SDK absent)

### To modify
- `src/gateway/app.py` — `app.include_router(chat_router)`
- `src/settings.py` — add `CHAT_DEFAULT_TIMEOUT_SECONDS: int = 120`, `CHAT_MAX_PROMPT_CHARS: int = 200_000`
- `pyproject.toml` — add `tiktoken==0.8.*` (already lightweight)
- `.env.example` — document new settings

### To delete
- (none)

## Implementation Steps

1. **Request schema** (`src/gateway/schemas/chat_request.py`)
   ```python
   class TextContent(BaseModel):
       type: Literal["text"]; text: str

   class ImageContent(BaseModel):
       type: Literal["image_url"]
       image_url: dict  # rejected at validator — vision not supported v1

   ContentPart = Annotated[Union[TextContent, ImageContent], Field(discriminator="type")]

   class Message(BaseModel):
       role: Literal["system", "user", "assistant"]
       content: str | list[ContentPart]
       name: str | None = None

   class StreamOptions(BaseModel):
       include_usage: bool = False
       model_config = ConfigDict(extra="forbid")

   class ChatCompletionRequest(BaseModel):
       model: str
       messages: list[Message] = Field(min_length=1)
       stream: bool = False
       stream_options: StreamOptions | None = None
       temperature: float | None = Field(default=None, ge=0, le=2)
       max_tokens: int | None = Field(default=None, gt=0)
       user: str | None = None
       # explicit reject list — any of these → 400
       n: int | None = None
       tools: list | None = None
       functions: list | None = None
       tool_choice: Any | None = None
       response_format: Any | None = None
       logprobs: bool | None = None
       top_logprobs: int | None = None
       seed: int | None = None  # accepted but ignored
       model_config = ConfigDict(extra="ignore")  # tolerate unknown fields silently (forward-compat)

       @model_validator(mode="after")
       def reject_unsupported(self):
           rejects = []
           if self.n is not None and self.n != 1: rejects.append("n>1")
           if self.tools: rejects.append("tools")
           if self.functions: rejects.append("functions")
           if self.tool_choice: rejects.append("tool_choice")
           if self.response_format: rejects.append("response_format")
           if self.logprobs: rejects.append("logprobs")
           if rejects: raise ValueError(f"unsupported: {','.join(rejects)}")
           return self
   ```

2. **Response schema** (`src/gateway/schemas/chat_response.py`)
   - `Usage(BaseModel)`: `prompt_tokens, completion_tokens, total_tokens` (ints).
   - `Message(BaseModel)`: `role`, `content`.
   - `Choice(BaseModel)`: `index`, `message`, `finish_reason`, `logprobs: None`.
   - `ChatCompletion(BaseModel)`: `id, object="chat.completion", created, model, choices, usage`.
   - `Delta(BaseModel)`: `role: str | None = None`, `content: str | None = None` (extra=allow).
   - `ChunkChoice(BaseModel)`: `index`, `delta`, `finish_reason: str | None`, `logprobs: None`.
   - `ChatCompletionChunk(BaseModel)`: `id, object="chat.completion.chunk", created, model, choices, usage: Usage | None = None`.

3. **Prompt builder** (`src/chat/prompt_builder.py`)
   ```python
   def build_prompt(messages: list[Message]) -> str:
       parts = []
       for m in messages:
           if isinstance(m.content, list):
               # only TextContent allowed (validator rejects images already)
               text = "".join(p.text for p in m.content if p.type == "text")
           else:
               text = m.content
           parts.append(f"{m.role.capitalize()}:\n{text}")
       return "\n\n".join(parts) + "\n\nAssistant:\n"
   ```
   Cap total length at `settings.CHAT_MAX_PROMPT_CHARS`; raise `ValueError` → handled to 400 in router.

4. **ID factory** (`src/chat/id_factory.py`)
   - `new_completion_id() -> str` returns `f"chatcmpl_{secrets.token_hex(13)}"` → 26 hex chars matching OpenAI shape closely (their format is opaque).

5. **Usage estimator** (`src/chat/usage_estimator.py`)
   ```python
   _enc = tiktoken.get_encoding("cl100k_base")
   def estimate(prompt_text: str, completion_text: str) -> Usage:
       p = len(_enc.encode(prompt_text))
       c = len(_enc.encode(completion_text))
       return Usage(prompt_tokens=p, completion_tokens=c, total_tokens=p+c)
   ```
   Pre-warm encoder at module import (faster first request).

6. **Sync handler** (`src/chat/sync_handler.py`)
   ```python
   async def handle_sync(req: ChatCompletionRequest, prompt: str, ws: Path) -> ChatCompletion:
       parts: list[str] = []
       finish = "stop"
       try:
           async for evt in run_codex(prompt, allow_write=False,
                                      workspace_dir=ws, timeout=settings.CHAT_DEFAULT_TIMEOUT_SECONDS):
               if isinstance(evt, ItemCompleted) and isinstance(evt.item, AgentMessageItem):
                   parts.append(evt.item.text)
               elif isinstance(evt, ErrorEvent):
                   logger.warning("chat.codex_error", code=evt.error.code, msg=evt.error.message)
                   finish = "error"
                   break
               elif isinstance(evt, TurnCompleted):
                   break
       except Exception:
           logger.exception("chat.sync_failure")
           raise
       text = "".join(parts)
       if req.max_tokens:
           # soft truncate by tokens
           text = _truncate_to_tokens(text, req.max_tokens)
           if finish == "stop" and len(text) < len("".join(parts)): finish = "length"
       usage = estimate(prompt, text)
       return ChatCompletion(
           id=new_completion_id(), object="chat.completion",
           created=int(time.time()), model=req.model,
           choices=[Choice(index=0, message=Message(role="assistant", content=text),
                           finish_reason=finish, logprobs=None)],
           usage=usage)
   ```

7. **Stream handler** (`src/chat/stream_handler.py`) — produces a raw byte iterator. Keepalive injection is the OUTER layer (route wraps with `sse_helpers.keepalive_wrap`).
   ```python
   async def stream_chunks(req: ChatCompletionRequest, prompt: str, ws: Path) -> AsyncIterator[bytes]:
       cid = new_completion_id()
       created = int(time.time())
       sent_role = False
       collected = []
       finish = "stop"
       error_msg: str | None = None

       def chunk(delta: dict, finish_reason: str | None = None,
                 usage: Usage | None = None, choices_empty: bool = False) -> bytes:
           c = ChatCompletionChunk(
               id=cid, object="chat.completion.chunk", created=created, model=req.model,
               choices=[] if choices_empty else [
                   ChunkChoice(index=0, delta=Delta(**delta),
                               finish_reason=finish_reason, logprobs=None)],
               usage=usage)
           return f"data: {c.model_dump_json(exclude_none=True)}\n\n".encode()

       try:
           async for evt in run_codex(prompt, allow_write=False,
                                      workspace_dir=ws, timeout=settings.CHAT_DEFAULT_TIMEOUT_SECONDS):
               if isinstance(evt, ItemCompleted) and isinstance(evt.item, AgentMessageItem):
                   piece = evt.item.text
                   collected.append(piece)
                   if not sent_role:
                       yield chunk({"role": "assistant", "content": piece}); sent_role = True
                   else:
                       yield chunk({"content": piece})
                   if req.max_tokens and _est_tokens("".join(collected)) >= req.max_tokens:
                       finish = "length"; break
               elif isinstance(evt, ErrorEvent):
                   logger.warning("chat.stream.codex_error", code=evt.error.code, msg=evt.error.message)
                   finish = "error"; error_msg = evt.error.message
                   break
               elif isinstance(evt, TurnCompleted):
                   break
       except asyncio.CancelledError:
           logger.info("chat.client_disconnect", id=cid)
           raise
       except Exception:
           logger.exception("chat.stream.unexpected")
           finish = "error"

       # ensure role chunk emitted even if codex produced no agent_message
       if not sent_role: yield chunk({"role": "assistant", "content": ""})
       # final chunk with finish_reason
       yield chunk({}, finish_reason=finish)
       # optional usage chunk
       if req.stream_options and req.stream_options.include_usage:
           yield chunk({}, usage=estimate(prompt, "".join(collected)), choices_empty=True)
       yield b"data: [DONE]\n\n"
   ```
   **Keepalive (MM1)**: caller wraps with `sse_helpers.keepalive_wrap(stream_chunks(...), interval=15.0)` so during long codex silence (>15s without an event), `: keepalive\n\n` comments are emitted to keep the connection alive across Caddy/CDN/AWS-ALB idle timers. The keepalive helper handles the timing internally — `stream_chunks` itself remains unaware.

8. **Router** (`src/gateway/routes/chat.py`) — addresses C3: ALL response headers are set HERE on `StreamingResponse(...)` construction; never injected post-hoc by middleware. Phase-06 rate-limit middleware writes `request.state.rate_limit_headers: dict[str, str]` BEFORE this route runs; we read + merge.
   ```python
   @router.post("/v1/chat/completions")
   async def chat_completions(req: ChatCompletionRequest, request: Request):
       try:
           prompt = build_prompt(req.messages)
       except ValueError as e:
           return openai_error(400, str(e), "invalid_request_error", "invalid_value")
       if len(prompt) > settings.CHAT_MAX_PROMPT_CHARS:
           return openai_error(400, "prompt too large", "invalid_request_error", "context_length_exceeded")

       job_id = str(uuid4())
       ws = make_workspace(job_id)
       # Merge rate-limit headers (set by phase-06 middleware on request.state) with SSE headers.
       # CRITICAL: must construct StreamingResponse with full header dict — adding headers via
       # BaseHTTPMiddleware after-the-fact would buffer the body (Starlette #1012, fastapi #5536).
       sse_headers = {
           "Cache-Control": "no-cache",
           "X-Accel-Buffering": "no",  # tells nginx not to buffer
           "Connection": "keep-alive",
           **getattr(request.state, "rate_limit_headers", {}),
       }
       try:
           if req.stream:
               raw_stream = stream_chunks(req, prompt, ws)
               # Wrap with keepalive util from phase-00 sse_helpers (MM1) — emits
               # `: keepalive\n\n` SSE comment every 15s during codex silence.
               kept = sse_helpers.keepalive_wrap(raw_stream, interval=15.0)
               return StreamingResponse(
                   kept,
                   media_type="text/event-stream",
                   headers=sse_headers,
                   background=BackgroundTask(cleanup_workspace, ws))
           result = await handle_sync(req, prompt, ws)
           return result
       finally:
           if not req.stream:  # stream cleanup runs via BackgroundTask
               cleanup_workspace(ws)
   ```
   Pydantic ValidationError caught by FastAPI default → 422; re-shape via custom exception handler in app factory to 400 OpenAI envelope.

   **Anti-pattern explicitly forbidden in this phase**: do NOT introduce a `BaseHTTPMiddleware` that mutates response headers on streaming responses. Phase-06 must use ASGI-direct middleware (`async def __call__(self, scope, receive, send)`) or stash headers in `request.state` for routes to consume — the second pattern is what we use here.

9. **App wiring** (`src/gateway/app.py`)
   - `app.include_router(chat_router)` (no prefix; route already absolute `/v1/chat/completions`).
   - Add `RequestValidationError` handler that returns OpenAI shape with `code="invalid_request_error"`.

10. **Tests**
    - **Request validation** unit: each rejected param → 400 with envelope. `stream_options.include_usage` accepted. Image content → 400. Empty messages → 422→400.
    - **Prompt builder** unit: multi-turn role formatting, image rejection, oversized prompt.
    - **Sync handler** unit: feed mocked async-iterator yielding events `[ThreadStarted, ItemCompleted(AgentMessageItem("hello ")), ItemCompleted(AgentMessageItem("world")), TurnCompleted]` → assert `content="hello world"`, `finish_reason="stop"`, usage tokens > 0.
    - **Sync handler** unit: mid-stream ErrorEvent → `finish_reason="error"`.
    - **Sync handler** unit: max_tokens enforcement → finish=length when truncated.
    - **Stream handler** unit: assert exact byte sequence of SSE for the same fixture: first chunk has `role`+`content`, middle has `content` only, final has `finish_reason="stop"`, `[DONE]` present. With `include_usage`, extra chunk has `choices: []` and `usage` populated (researcher-02 §A.3).
    - **Stream handler** unit: client cancel → raises CancelledError up; cleanup task fires.
    - **Keepalive sentinel test** (MM1): mock `run_codex` async-iterator that yields one event then sleeps 30s before next; assert `: keepalive\n\n` byte sequence appears between deltas.
    - **C3 sentinel test** (real-uvicorn streaming integration test, NOT TestClient): boots a real uvicorn worker on a random port, sends `stream: true` request, asserts FIRST SSE byte arrives within 1.0s of the codex subprocess emitting its first event. TestClient buffers and gives false-positives — this test catches BaseHTTPMiddleware regressions. Lives in `tests/integration/test_chat_streaming_no_buffering.py`. Skipped if codex auth not present; runs always against mock-codex fixture.
    - **Integration e2e** (skipped without auth): real codex with prompt `"reply with the single word: pong"` → `content == "pong"` (case-insensitive match).
    - **OpenAI SDK compat** (`tests/compat/`): `client.chat.completions.create(...)` non-stream returns parseable object; stream iteration yields chunks with expected attributes.

11. **Local verification**
    - `curl -N -H "Authorization: Bearer cwk_..." -H "Content-Type: application/json" \
       -d '{"model":"codex-cli","messages":[{"role":"user","content":"say hi"}],"stream":true}' \
       http://localhost:8000/v1/chat/completions` → SSE bytes; pipe through `| grep -c '^data: '` ≥ 3.
    - Same without `stream:true` → JSON with `choices[0].message.content` populated.
    - Send `n: 2` → 400 envelope.

## Todo List
- [ ] `src/gateway/schemas/chat_request.py` with explicit reject validator
- [ ] `src/gateway/schemas/chat_response.py` (Completion + Chunk shapes)
- [ ] `src/chat/prompt_builder.py`
- [ ] `src/chat/id_factory.py`
- [ ] `src/chat/usage_estimator.py` (tiktoken)
- [ ] `src/chat/sync_handler.py`
- [ ] `src/chat/stream_handler.py` (data-only SSE; include_usage extra chunk; [DONE] terminator)
- [ ] Route wraps `stream_chunks` with `sse_helpers.keepalive_wrap(..., interval=15.0)` (MM1)
- [ ] Route reads `request.state.rate_limit_headers` and includes them in `StreamingResponse(headers=...)` (C3 — never injected by middleware post-hoc)
- [ ] `src/gateway/routes/chat.py` (sync + stream branching; workspace cleanup)
- [ ] Sentinel test `test_chat_streaming_no_buffering.py` runs against real uvicorn (not TestClient); first SSE byte arrives < 1s
- [ ] Keepalive sentinel test: 30s codex silence yields `: keepalive\n\n` between deltas
- [ ] `app.py` wiring + RequestValidationError handler → OpenAI envelope
- [ ] Settings: `CHAT_DEFAULT_TIMEOUT_SECONDS`, `CHAT_MAX_PROMPT_CHARS`
- [ ] `tiktoken==0.8.*` in pyproject
- [ ] Unit tests (validation, builder, sync, stream byte-sequence, cancel)
- [ ] Integration e2e (real codex, skipped if no auth)
- [ ] OpenAI Python SDK smoke compat test
- [ ] Manual curl verification

## Success Criteria
- Sync `POST /v1/chat/completions` returns body shape that openai-python `ChatCompletion.model_validate(...)` accepts without error.
- Stream returns SSE bytes that openai-python `Stream[ChatCompletionChunk]` iterates without error; ≥ 1 chunk with `delta.role="assistant"`, ≥ 1 with `delta.content`, exactly 1 with `finish_reason="stop"`, terminated by `data: [DONE]\n\n`.
- `include_usage: true` produces an extra chunk with `choices: []` + `usage` populated (researcher-02 §A.3 final-with-usage exact shape).
- Rejected fields return 400 with OpenAI error envelope; type=`invalid_request_error`.
- Workspace dir created and cleaned up per request — no leaks after 100 requests (integration loop test).
- Client disconnect propagates SIGTERM to codex within `JOB_CANCEL_GRACE_SECONDS`; no orphan processes.
- p95 first-token latency < 2s on warm stream (success metric verified by perf test in phase 09).
- Sentinel test (`tests/integration/test_chat_streaming_no_buffering.py`) running against real uvicorn proves first SSE byte arrives within 1.0s of subprocess first event (C3 regression guard).
- During a 30s+ codex silence, client receives `: keepalive\n\n` SSE comments at ~15s cadence (MM1 regression guard).
- All Python files ≤ 200 LOC; ruff + mypy clean.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| openai-python rejects our chunk shape | M | HIGH | Compat test in `tests/compat/` runs against real SDK on every CI; fail build on parse error. |
| Codex never emits `agent_message` (only reasoning/tool_use) | M | M | Stream handler always emits role chunk + final chunk even with empty content; sync returns empty string + `finish_reason="stop"`. |
| `finish_reason="error"` rejected by strict OpenAI clients | L | M | Documented deviation. Fallback: emit only the OpenAI-canonical `"stop"` and rely on log for error trace. Decision: ship with `"error"` first, narrow to `"stop"` if compat test fails. |
| tiktoken misses model-specific tokenization | L | L | We use `cl100k_base` for all — best-effort, documented. Codex tokens are unknowable from outside. |
| Mid-stream codex error leaks stderr to client | L | M | Phase 02 caps stderr at 64 KiB and only includes 4 KiB tail in synthesized ErrorEvent; we log it but don't stream it to client. Final chunk has generic `finish_reason="error"`. |
| StreamingResponse buffers due to proxy | M | M | Headers `X-Accel-Buffering: no`, `Cache-Control: no-cache` set; document Caddy `flush_interval -1` requirement (phase 10). |
| BaseHTTPMiddleware buffers SSE body, breaking streaming | ~~HIGH~~ → resolved | ~~HIGH~~ | **Addressed via C3**: all response headers set in route handler on `StreamingResponse(headers=...)` construction; phase-06 rate-limit middleware writes to `request.state.rate_limit_headers` BEFORE `call_next`, route reads + merges. UsageTracking moved to ASGI-direct or non-streaming-only. Sentinel test (real uvicorn, not TestClient) asserts first byte < 1s. |
| Slow stream killed by Caddy/CDN/AWS-ALB idle timeout (>30s silence) | ~~M~~ → resolved | ~~HIGH~~ | **Addressed via MM1**: `sse_helpers.keepalive_wrap(stream_chunks, interval=15.0)` emits `: keepalive\n\n` SSE comments during silence. 15s cadence is half typical 30s idle defaults. |
| `--ephemeral` flag missing from 0.125.0 | L | M | **Addressed via C1**: phase-02 runner reads `settings.CODEX_HAS_EPHEMERAL` set by phase-00 verify-codex; this phase doesn't construct argv. |
| Prompt construction loses critical separation | M | M | Format per PDF skeleton (`Role:\ncontent`), unit tested across multi-turn cases; if LLMs confuse boundaries, switch to JSON-encoded transcript (defer). |
| Workspace cleanup fails mid-stream → disk fill | L | M | `BackgroundTask` runs after response close; cleanup is idempotent + logged. Phase 08 adds periodic GC of `WORKSPACE_ROOT/*` older than 1h. |
| max_tokens truncation cuts mid-UTF-8 | L | L | tiktoken-based truncation works on token boundaries → safe. |
| Vision content sent → 400 confuses SDK retry | L | L | OpenAI envelope `code: invalid_request_error` is standard; SDK won't retry. |

## Security Considerations
- Sandbox `read-only` (phase 02) — chat-completions cannot write to workspace, cannot reach network.
- `--ephemeral` ensures no thread state persists to disk between requests; matches stateless OpenAI semantics + reduces disk attrition.
- No client-supplied `cwd`, `--sandbox`, `--ask-for-approval`, or extra flags ever reach codex args (locked anti-pattern, brainstorm §10).
- Prompt injection cannot escape sandbox: even if user prompt says "delete /etc", `read-only` blocks writes; stream-handler doesn't execute the response, just relays text.
- Auth required (middleware); `request.state.user_id` available for audit log (phase 08 records `prompt_hash` not raw prompt).
- Streaming response length unbounded in absence of `max_tokens` — server-side cap by `JOB_TIMEOUT_SECONDS` (phase 02) is the ultimate guard. Per-tier RPM/TPM caps land in phase 06.
- structlog redactor (phase 0) ensures any inadvertent log of message content with embedded API key is scrubbed.
- Error responses never include codex stderr or stack traces — only an opaque message + code. Detailed cause stays in server logs.

## Next Steps
- Phase 04 builds `/v1/responses` against the same `run_codex` iterator but maps to the richer event taxonomy (researcher-02 part B) including `event:` lines + `sequence_number` ordering.
- Phase 06 adds per-request rate-limit checks BEFORE entering this handler (RPM + concurrent caps); this phase's `request.state.api_key.tier` is consumed there.
- Phase 08 adds: audit-log row per completion (user_id, prompt_hash, input/output tokens, duration_ms); workspace GC; secret-rotation runbook.
- Phase 09 SDK compat suite formally validates byte-for-byte parity against OpenAI Python + Node SDKs.
