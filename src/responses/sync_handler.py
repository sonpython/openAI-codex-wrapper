"""
Sync (non-streaming) Responses API handler.

Drives the Codex event stream to completion and collects output into
a single ResponseObject. Does NOT own workspace lifecycle — caller handles.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog

from src.chat.usage_estimator import estimate
from src.codex.events import (
    AgentMessageItem,
    CodexEvent,
    ErrorEvent,
    ItemCompleted,
    TurnCompleted,
    TurnFailed,
)
from src.gateway.schemas.responses_object import (
    OutputItem,
    OutputTextContent,
    OutputTokensDetails,
    ResponseError,
    ResponseObject,
    ResponseUsage,
)

logger = structlog.get_logger(__name__)


async def collect_response(
    events: AsyncIterator[CodexEvent],
    *,
    response_id: str,
    model: str,
    created_at: str,
    prompt: str,
    metadata: dict[str, str] | None = None,
) -> ResponseObject:
    """Drain Codex event stream and return a complete ResponseObject.

    Args:
        events:      Async iterator of typed Codex events.
        response_id: Pre-generated ``resp_<hex>`` ID.
        model:       Model name from request.
        created_at:  ISO-8601 UTC timestamp string.
        prompt:      Full assembled prompt (for token estimation).
        metadata:    Optional metadata dict.

    Returns:
        ResponseObject with status "completed", "failed", or "cancelled".
    """
    parts: list[str] = []
    codex_usage: ResponseUsage | None = None
    failed = False
    error: ResponseError | None = None

    try:
        async for evt in events:
            if isinstance(evt, ItemCompleted) and isinstance(evt.item, AgentMessageItem):
                parts.append(evt.item.text)

            elif isinstance(evt, TurnCompleted):
                if evt.usage:
                    codex_usage = ResponseUsage(
                        input_tokens=evt.usage.input_tokens,
                        output_tokens=evt.usage.output_tokens,
                        total_tokens=evt.usage.input_tokens + evt.usage.output_tokens,
                        output_tokens_details=OutputTokensDetails(
                            reasoning_tokens=evt.usage.reasoning_tokens
                        ),
                    )
                break

            elif isinstance(evt, TurnFailed):
                failed = True
                err_msg = str(evt.error) if evt.error else "turn failed"
                error = ResponseError(code="server_error", message=err_msg)
                logger.warning("responses.sync.turn_failed", error=evt.error)
                break

            elif isinstance(evt, ErrorEvent):
                failed = True
                code = evt.error.code
                openai_code = "timeout" if code == "TIMEOUT" else "server_error"
                error = ResponseError(code=openai_code, message=evt.error.message)
                logger.warning(
                    "responses.sync.codex_error",
                    code=code,
                    message=evt.error.message,
                )
                break

    except Exception:
        logger.exception("responses.sync.unexpected_error", response_id=response_id)
        raise

    if failed:
        return ResponseObject(
            id=response_id,
            created_at=created_at,
            status="failed",
            model=model,
            output=[],
            error=error,
            metadata=metadata or None,
        )

    full_text = "".join(parts)

    # Use tiktoken estimate when codex usage is unavailable (common in v1).
    if codex_usage is None:
        chat_usage = estimate(prompt, full_text)
        codex_usage = ResponseUsage(
            input_tokens=chat_usage.prompt_tokens,
            output_tokens=chat_usage.completion_tokens,
            total_tokens=chat_usage.total_tokens,
        )

    output_items: list[OutputItem] = []
    if full_text:
        item_id = f"item_{response_id[5:]}"  # reuse resp hex suffix
        output_items.append(
            OutputItem(
                id=item_id,
                type="message",
                status="completed",
                role="assistant",
                content=[OutputTextContent(type="output_text", text=full_text)],
            )
        )

    return ResponseObject(
        id=response_id,
        created_at=created_at,
        status="completed",
        model=model,
        output=output_items,
        usage=codex_usage,
        metadata=metadata or None,
    )
