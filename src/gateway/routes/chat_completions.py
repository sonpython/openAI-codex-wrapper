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

from uuid import uuid4

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from src.chat.prompt_builder import build_prompt
from src.chat.stream_handler import stream_chunks
from src.chat.sync_handler import handle_sync
from src.codex.runner import run_codex
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
    try:
        prompt = build_prompt(req.messages)
    except ValueError as exc:
        return _openai_error(400, str(exc), code="context_length_exceeded")

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
        runner = run_codex(
            prompt,
            allow_write=False,
            workspace_dir=ws,
            timeout=timeout,
            request_id=job_id,
        )
        raw_stream = stream_chunks(req, prompt, runner)
        kept = keepalive_wrap(raw_stream, interval=15.0)

        return StreamingResponse(
            kept,
            media_type="text/event-stream",
            headers=sse_headers,
            background=BackgroundTask(cleanup_workspace, ws),
        )

    # ── Sync path ──────────────────────────────────────────────────────────
    try:
        runner = run_codex(
            prompt,
            allow_write=False,
            workspace_dir=ws,
            timeout=timeout,
            request_id=job_id,
        )
        result = await handle_sync(req, prompt, runner)
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
