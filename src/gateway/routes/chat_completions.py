"""
Route handler for POST /v1/chat/completions.

C3 contract: ALL SSE response headers are set here on StreamingResponse
construction — never injected post-hoc by BaseHTTPMiddleware (which buffers
StreamingResponse bodies, breaking streaming per Starlette #1012).

Phase-06 rate-limit middleware will populate ``request.state.rate_limit_headers``
before this route runs; we read + merge into the StreamingResponse headers.

MM1 contract: SSE generator is wrapped with ``keepalive_wrap(interval=15.0)``
so `: keepalive\\n\\n` comments are emitted during long Codex silences (>15s)
to prevent Caddy/CDN/AWS-ALB idle-timeout kills.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from uuid import uuid4

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from src.chat.error_chunk import synth_error_chunk
from src.chat.prompt_builder import build_prompt
from src.chat.stream_handler import stream_chunks
from src.chat.sync_handler import handle_sync
from src.chat.tool_calling import format_tools_prompt
from src.chat.usage_estimator import _count_tokens
from src.codex.runner import resolve_sandbox_flag, run_codex
from src.codex.workspace import cleanup_workspace, make_workspace
from src.gateway.schemas.chat_request import ChatCompletionRequest
from src.gateway.sse_helpers import keepalive_wrap
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter()

# ── Error envelope helper ──────────────────────────────────────────────────────


def _openai_error(
    status: int,
    message: str,
    error_type: str = "invalid_request_error",
    code: str = "invalid_value",
    param: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )


# ── Route ──────────────────────────────────────────────────────────────────────


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
) -> StreamingResponse | JSONResponse:
    """Handle sync and streaming chat completion requests.

    Pydantic validation (including unsupported-field rejection) is performed
    by FastAPI before this function is called. ValidationErrors are caught by
    the app-level handler in ``app.py`` and reshaped to OpenAI 400 envelopes.
    """
    settings = get_settings()

    # Build prompt — may raise ValueError (too long) → caught below → 400.
    # When tools are present, format_tools_prompt generates a system-level
    # instruction block that is prepended to the prompt so Codex knows to
    # emit structured JSON for tool calls.
    try:
        tools_prompt = format_tools_prompt(req.tools, req.tool_choice) if req.tools else None
        prompt = build_prompt(req.messages, tools_prompt=tools_prompt)
    except ValueError as exc:
        return _openai_error(400, str(exc), code="context_length_exceeded")

    # Mode dispatch: read execution mode set by AuthMiddleware (default "sandbox").
    # local-bridge mode is not yet implemented — short-circuit with 501 before
    # any workspace is created or runner is spawned.
    api_mode: str = getattr(request.state, "codex_mode", "sandbox")
    if api_mode == "local-bridge":
        return _openai_error(
            501,
            "local-bridge mode is not yet implemented; use sandbox or vps.",
            error_type="api_error",
            code="local_bridge_not_implemented",
        )
    # MEDIUM-3: catch unknown future modes before spawning any workspace/runner.
    try:
        sandbox_flag = resolve_sandbox_flag(api_mode)
    except ValueError:
        return _openai_error(
            501,
            f"execution mode {api_mode!r} is not supported by this gateway version",
            error_type="not_implemented",
            code="unsupported_mode",
        )

    job_id = str(uuid4())

    # Workspace creation — raises CodexRunnerError if root missing.
    try:
        ws = make_workspace(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat.workspace.create_failed", job_id=job_id)
        return _openai_error(500, str(exc), error_type="api_error", code="internal_error")

    # C3: merge phase-06 rate-limit headers (stashed in request.state before
    # call_next) with mandatory SSE headers. Both sets set at construction time.
    rl_headers: dict[str, str] = getattr(request.state, "rate_limit_headers", {})
    sse_headers: dict[str, str] = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        **rl_headers,
    }

    timeout = float(settings.chat_default_timeout_seconds)

    if req.stream:
        # ── Streaming path ─────────────────────────────────────────────────
        # Runner is an async-generator; stream_chunks wraps it into SSE bytes;
        # keepalive_wrap injects `: keepalive\n\n` comments during silence.
        #
        # C3 fix: Workspace cleanup is registered as a Starlette BackgroundTask
        # (singular) so it runs after the response body is fully sent — even if
        # Starlette aborts before the consumer starts iteration (e.g. an error
        # thrown before the first chunk reaches the client).  Generator-finally
        # alone doesn't guarantee cleanup in that scenario.
        #
        # C1 fix: Wrap the stream to capture usage after [DONE] is emitted so
        # UsageTrackingMiddleware can read request.state.usage for TPM true-up
        # and monthly quota increment.  Prompt tokens are estimated once;
        # completion tokens are derived from the SSE output bytes (rough).
        runner = run_codex(
            prompt,
            sandbox_mode=sandbox_flag,
            workspace_dir=ws,
            timeout=timeout,
            request_id=job_id,
        )
        raw_stream = stream_chunks(req, prompt, runner)
        kept = keepalive_wrap(raw_stream, interval=15.0)
        # Capture created/cid for the synthetic error chunk — must be stable
        # across the lifetime of the generator (closed over below).
        _created = int(time.time())
        _cid = f"chatcmpl_{job_id}"

        async def _stream_with_usage_capture() -> AsyncIterator[bytes]:
            """Exhaust `kept` and write usage to request.state after stream ends.

            We accumulate the text content bytes from SSE data lines to estimate
            completion tokens.  This is an approximation — the canonical
            token count comes from the tiktoken counter inside stream_chunks —
            but it is sufficient for TPM true-up purposes.

            Phase-3 fix: on any exception from the upstream iterator, emit a
            synthetic terminal chunk (finish_reason='error') + '[DONE]' so that
            aiohttp clients receive a clean chunked-transfer EOF instead of
            raising TransferEncodingError 400.  We do NOT re-raise — Starlette
            must see a clean generator return to close the body correctly.
            """
            completion_bytes = 0
            sent_done = False
            try:
                async for chunk in kept:
                    # Count non-keepalive SSE payload bytes as a proxy for tokens.
                    if chunk.startswith(b"data: ") and not chunk.startswith(b"data: [DONE]"):
                        completion_bytes += len(chunk)
                    if chunk.startswith(b"data: [DONE]"):
                        sent_done = True
                    yield chunk
            except Exception:
                logger.exception("chat.stream.wrapper_failed", job_id=job_id)
                if not sent_done:
                    # Flush synthetic terminal frame so chunked body closes cleanly.
                    yield synth_error_chunk(req.model, _cid, _created)
                    yield b"data: [DONE]\n\n"
                    sent_done = True
                # Do NOT re-raise — let StreamingResponse close cleanly.
            finally:
                # Estimate from prompt + completion output bytes.
                prompt_tokens = _count_tokens(prompt)
                completion_tokens = max(1, completion_bytes // 4)
                request.state.usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                }

        return StreamingResponse(
            _stream_with_usage_capture(),
            media_type="text/event-stream",
            headers=sse_headers,
            background=BackgroundTask(cleanup_workspace, ws),
        )

    # ── Sync path ──────────────────────────────────────────────────────────
    try:
        runner = run_codex(
            prompt,
            sandbox_mode=sandbox_flag,
            workspace_dir=ws,
            timeout=timeout,
            request_id=job_id,
        )
        result = await handle_sync(req, prompt, runner)
        # C1: expose actual token counts so UsageTrackingMiddleware can true-up
        # TPM and increment monthly quota.  sync_handler.estimate() populates
        # result.usage with prompt_tokens + completion_tokens.
        usage = result.usage
        request.state.usage = {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
        return JSONResponse(content=result.model_dump())
    except Exception:
        logger.exception("chat.sync.unhandled_error", job_id=job_id)
        return _openai_error(
            500,
            "An internal error occurred.",
            error_type="api_error",
            code="internal_error",
        )
    finally:
        cleanup_workspace(ws)
