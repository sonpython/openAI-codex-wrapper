"""
Sync (non-streaming) chat completion handler.

Collects all ``agent_message`` text from the Codex event stream and
assembles a single ``ChatCompletion`` response object. Handles:
  - Normal completion (TurnCompleted → finish_reason="stop")
  - Codex error mid-stream (ErrorEvent → finish_reason="error")
  - max_tokens soft cap (tiktoken truncation → finish_reason="length")
  - Unexpected exceptions propagated to caller (route handler → 500)

Does NOT own the workspace lifecycle — caller creates and cleans up.
Does NOT own the runner — caller passes the async iterator.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import structlog

from src.chat.id_factory import new_completion_id
from src.chat.usage_estimator import estimate, truncate_to_tokens
from src.codex.events import (
    AgentMessageItem,
    CodexEvent,
    ErrorEvent,
    ItemCompleted,
    TurnCompleted,
    TurnFailed,
)
from src.gateway.schemas.chat_request import ChatCompletionRequest
from src.gateway.schemas.chat_response import (
    ChatCompletion,
    Choice,
    ResponseMessage,
)

logger = structlog.get_logger(__name__)


async def handle_sync(
    req: ChatCompletionRequest,
    prompt: str,
    events: AsyncIterator[CodexEvent],
) -> ChatCompletion:
    """Collect Codex events and return a complete ChatCompletion.

    Args:
        req:    Validated request (used for model, max_tokens).
        prompt: Assembled prompt string (used for token estimation).
        events: Async iterator of typed CodexEvent objects from the runner.

    Returns:
        ChatCompletion — ready to serialise as JSON response.

    Raises:
        Exception: Any unexpected error from the event iterator is re-raised
                   after logging; the route converts it to a 500.
    """
    parts: list[str] = []
    finish: str = "stop"

    try:
        async for evt in events:
            if isinstance(evt, ItemCompleted) and isinstance(evt.item, AgentMessageItem):
                parts.append(evt.item.text)
            elif isinstance(evt, ErrorEvent):
                logger.warning(
                    "chat.sync.codex_error",
                    code=evt.error.code,
                    message=evt.error.message,
                )
                finish = "error"
                break
            elif isinstance(evt, TurnCompleted):
                break

            elif isinstance(evt, TurnFailed):
                # H4: TurnFailed is a terminal event from the runner — treat as error.
                logger.warning(
                    "chat.sync.turn_failed",
                    error=getattr(evt, "error", None),
                )
                finish = "error"
                break
            # All other event types (TurnStarted, ItemStarted, etc.) are skipped.
    except Exception:
        logger.exception("chat.sync.unexpected_error")
        raise

    text = "".join(parts)

    # Soft max_tokens cap — truncate at token boundary, update finish_reason.
    if req.max_tokens and text:
        truncated = truncate_to_tokens(text, req.max_tokens)
        if len(truncated) < len(text):
            text = truncated
            if finish == "stop":
                finish = "length"

    usage = estimate(prompt, text)

    return ChatCompletion(
        id=new_completion_id(),
        object="chat.completion",
        created=int(time.time()),
        model=req.model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(role="assistant", content=text),
                finish_reason=finish,
                logprobs=None,
            )
        ],
        usage=usage,
    )
