"""
Sync (non-streaming) chat completion handler.

Collects all ``agent_message`` text from the Codex event stream and
assembles a single ``ChatCompletion`` response object. Handles:
  - Normal completion (TurnCompleted → finish_reason="stop")
  - Codex error mid-stream (ErrorEvent → finish_reason="error")
  - max_tokens soft cap (tiktoken truncation → finish_reason="length")
  - Tool calls: when ``req.tools`` is non-empty, attempts to parse Codex
    output as a tool_calls JSON blob (finish_reason="tool_calls"); falls
    back to plain text on any parse failure (finish_reason="stop").
  - Unexpected exceptions propagated to caller (route handler → 500)

Does NOT own the workspace lifecycle — caller creates and cleans up.
Does NOT own the runner — caller passes the async iterator.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import structlog

from src.chat.id_factory import make_tool_call_id, new_completion_id
from src.chat.tool_calling import parse_tool_response
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
    ChatCompletionMessage,
    Choice,
    ResponseMessage,
    ToolCall,
    ToolCallFunction,
)

logger = structlog.get_logger(__name__)


async def handle_sync(
    req: ChatCompletionRequest,
    prompt: str,
    events: AsyncIterator[CodexEvent],
) -> ChatCompletion:
    """Collect Codex events and return a complete ChatCompletion.

    When ``req.tools`` is non-empty the handler attempts to parse the
    assembled text as a ``{"tool_calls": [...]}`` JSON object.  On success
    the response carries ``finish_reason="tool_calls"`` and a
    ``ChatCompletionMessage`` with ``content=None`` and populated
    ``tool_calls``.  On any parse / validation failure the response falls
    back to plain text with ``finish_reason="stop"``.

    Args:
        req:    Validated request (used for model, max_tokens, tools).
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
                # TurnFailed is a terminal event from the runner — treat as error.
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
    completion_id = new_completion_id()

    # ── Tool-calling branch ────────────────────────────────────────────────
    # Only attempt when tools were requested AND Codex returned something (not
    # an error turn). On any parse failure fall through to plain text path.
    if req.tools and finish == "stop" and text:
        valid_names = {
            t["function"]["name"]
            for t in req.tools
            if isinstance(t, dict) and isinstance(t.get("function"), dict)
        }
        parsed = parse_tool_response(text, valid_names)
        if parsed:
            tool_calls = [
                ToolCall(
                    id=make_tool_call_id(),
                    function=ToolCallFunction(
                        name=call["name"],
                        arguments=json.dumps(call["arguments"]),
                    ),
                )
                for call in parsed
            ]
            logger.info(
                "chat.sync.tool_calls_parsed",
                count=len(tool_calls),
                names=[tc.function.name for tc in tool_calls],
            )
            return ChatCompletion(
                id=completion_id,
                object="chat.completion",
                created=int(time.time()),
                model=req.model,
                choices=[
                    Choice(
                        index=0,
                        message=ChatCompletionMessage(
                            content=None,
                            tool_calls=tool_calls,
                        ),
                        finish_reason="tool_calls",
                        logprobs=None,
                    )
                ],
                usage=usage,
            )
        # Parse failed → log and fall through to plain text
        logger.debug("chat.sync.tool_parse_fallback", text_preview=text[:120])

    # ── Plain text path (default) ──────────────────────────────────────────
    return ChatCompletion(
        id=completion_id,
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
