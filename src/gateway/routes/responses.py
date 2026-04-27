"""
Route handler for POST /v1/responses.

C3 contract: ALL SSE headers set on StreamingResponse construction —
never injected by BaseHTTPMiddleware (Starlette #1012 / FastAPI #5536).

MM1 contract: SSE generator wrapped with keepalive_wrap(interval=15.0).

Workspace cleanup via BackgroundTask (covers early-abort path).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from src.chat.usage_estimator import _count_tokens
from src.codex.runner import run_codex
from src.codex.workspace import cleanup_workspace, make_workspace
from src.gateway.schemas.responses_request import ResponsesRequest
from src.gateway.sse_helpers import keepalive_wrap
from src.responses.events_emitter import ResponseEmitter
from src.responses.responses_helpers import build_responses_prompt
from src.responses.stream_handler import stream_responses
from src.responses.sync_handler import collect_response
from src.settings import get_settings

logger = structlog.get_logger(__name__)
router = APIRouter()


def _openai_error(
    status: int,
    message: str,
    error_type: str = "invalid_request_error",
    code: str = "invalid_request_error",
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


def _make_response_id() -> str:
    """Generate a ``resp_<26 hex>`` response ID."""
    return f"resp_{secrets.token_hex(13)}"


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.post("/v1/responses", response_model=None)
async def create_response(
    req: ResponsesRequest,
    request: Request,
) -> StreamingResponse | JSONResponse:
    """Handle sync and streaming Responses API requests.

    Pydantic validation (including unsupported-field rejection) fires before
    this function runs. ValueError messages from model_validator are caught
    by the app-level RequestValidationError handler and reshaped to 400.

    Special handling: unsupported_parameter errors from ResponsesRequest
    include a structured prefix so we can emit the correct OpenAI code+param.
    """
    settings = get_settings()

    try:
        prompt = build_responses_prompt(
            req.input, req.instructions, settings.responses_max_input_chars
        )
    except ValueError as exc:
        return _openai_error(400, str(exc), code="context_length_exceeded")

    response_id = _make_response_id()
    created_at = _iso_now()

    try:
        ws = make_workspace(response_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("responses.workspace.create_failed", response_id=response_id)
        return _openai_error(500, str(exc), error_type="api_error", code="internal_error")

    rl_headers: dict[str, str] = getattr(request.state, "rate_limit_headers", {})
    sse_headers: dict[str, str] = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        **rl_headers,
    }

    timeout = float(settings.responses_timeout_seconds)
    metadata = req.metadata or {}

    if req.stream:
        runner = run_codex(
            prompt,
            allow_write=False,
            workspace_dir=ws,
            timeout=timeout,
            model=req.model if req.model != "codex-cli" else None,
            request_id=response_id,
        )
        emitter = ResponseEmitter(
            response_id=response_id,
            model=req.model,
            created_at=created_at,
            metadata=metadata,
        )
        raw_stream = stream_responses(runner, emitter=emitter, request=request)
        kept = keepalive_wrap(raw_stream, interval=15.0)

        # C1 fix: capture usage after stream completes so UsageTrackingMiddleware
        # can true-up TPM and increment the monthly counter.
        async def _stream_with_usage_capture() -> AsyncIterator[bytes]:
            completion_bytes = 0
            async for chunk in kept:
                if not chunk.startswith(b": keepalive"):
                    completion_bytes += len(chunk)
                yield chunk
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

    # ── Sync path ─────────────────────────────────────────────────────────────
    try:
        runner = run_codex(
            prompt,
            allow_write=False,
            workspace_dir=ws,
            timeout=timeout,
            model=req.model if req.model != "codex-cli" else None,
            request_id=response_id,
        )
        response_obj = await collect_response(
            runner,
            response_id=response_id,
            model=req.model,
            created_at=created_at,
            prompt=prompt,
            metadata=metadata,
        )
        # C1: set actual token counts for UsageTrackingMiddleware true-up.
        if response_obj.usage is not None:
            usage = response_obj.usage
            request.state.usage = {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            }
        return JSONResponse(
            content=response_obj.model_dump(exclude_none=True),
            background=BackgroundTask(cleanup_workspace, ws),
        )
    except Exception:
        logger.exception("responses.sync.unhandled_error", response_id=response_id)
        cleanup_workspace(ws)
        return _openai_error(
            500,
            "An internal error occurred.",
            error_type="api_error",
            code="internal_error",
        )
