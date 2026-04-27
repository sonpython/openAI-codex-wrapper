"""
SSE streaming handler for POST /v1/chat/completions.

Produces a raw byte async-iterator of SSE ``data:`` lines. The route layer
wraps this with ``sse_helpers.keepalive_wrap`` (MM1) before returning a
``StreamingResponse`` — this module is unaware of keepalive timing.

SSE format (data-only, no ``event:`` line — chat-completions vs responses API):
    data: {json}\\n\\n
    ...
    data: [DONE]\\n\\n

Chunk evolution (researcher-02 §A.3):
  1. First chunk:  delta={role, content} (combined per OpenAI behavior)
  2. Middle chunks: delta={content}
  3. Final chunk:  delta={}, finish_reason=stop|length|error
  4. Usage chunk (if include_usage): choices=[], usage={...}
  5. [DONE] terminator

Deviation from OpenAI: on ErrorEvent we emit finish_reason="error" then
[DONE] rather than silently closing (documented; phase-09 validates compat).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import structlog

from src.chat.id_factory import new_completion_id
from src.chat.usage_estimator import _count_tokens
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
    ChatCompletionChunk,
    ChunkChoice,
    Delta,
    Usage,
)

logger = structlog.get_logger(__name__)


def _build_usage(prompt: str, completion_tokens: int) -> Usage:
    """Build a Usage object using a pre-computed completion token count.

    Called once at stream end for the include_usage chunk.  Prompt tokens are
    computed once here; completion_tokens come from the running O(N) counter
    maintained in stream_chunks (H5 fix — avoids O(N²) re-encode).
    """
    p = _count_tokens(prompt)
    return Usage(
        prompt_tokens=p,
        completion_tokens=completion_tokens,
        total_tokens=p + completion_tokens,
        _estimated=True,  # type: ignore[call-arg]  # extra field via extra=allow
    )


async def stream_chunks(
    req: ChatCompletionRequest,
    prompt: str,
    events: AsyncIterator[CodexEvent],
) -> AsyncIterator[bytes]:
    """Yield SSE bytes from a Codex event stream.

    Args:
        req:    Validated request (model, max_tokens, stream_options).
        prompt: Assembled prompt string for token estimation.
        events: Async iterator of typed CodexEvent objects from the runner.

    Yields:
        SSE-formatted bytes: ``b"data: {...}\\n\\n"`` or ``b"data: [DONE]\\n\\n"``.
    """
    cid = new_completion_id()
    created = int(time.time())
    sent_role = False
    # H5 fix: maintain running completion token counter (O(N) instead of O(N²)).
    # Increment by _count_tokens(piece) per chunk rather than re-encoding the
    # entire accumulated text on every iteration.
    _completion_tokens: int = 0
    finish = "stop"

    def _make_chunk(
        delta: dict[str, str | None],
        finish_reason: str | None = None,
        *,
        choices_empty: bool = False,
        completion_tokens: int = 0,
    ) -> bytes:
        chunk = ChatCompletionChunk(
            id=cid,
            object="chat.completion.chunk",
            created=created,
            model=req.model,
            choices=(
                []
                if choices_empty
                else [
                    ChunkChoice(
                        index=0,
                        delta=Delta(**delta),
                        finish_reason=finish_reason,
                        logprobs=None,
                    )
                ]
            ),
            # Usage computed once at the end (choices_empty=True), not per chunk.
            # H5: use the running completion_tokens counter passed in — avoids
            # re-encoding the full accumulated text on every chunk (O(N) total).
            usage=(_build_usage(prompt, completion_tokens) if choices_empty else None),
        )
        # exclude_none keeps wire format clean (no "usage": null on content chunks)
        return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n".encode()

    try:
        async for evt in events:
            if isinstance(evt, ItemCompleted) and isinstance(evt.item, AgentMessageItem):
                piece = evt.item.text

                if not sent_role:
                    # First content chunk: include role + content together.
                    yield _make_chunk({"role": "assistant", "content": piece})
                    sent_role = True
                else:
                    yield _make_chunk({"content": piece})

                # H5: increment running counter by just this piece (O(1) per chunk).
                _completion_tokens += _count_tokens(piece)

                # C2 fix: Soft max_tokens cap — stop emitting AFTER the current piece
                # is already sent.  Do NOT re-emit a "corrected" delta (that would
                # ADD bytes on top of what the client already received).  Simply set
                # finish="length" and break so the final chunk carries the right reason.
                if req.max_tokens and _completion_tokens >= req.max_tokens:
                    finish = "length"
                    break

            elif isinstance(evt, ErrorEvent):
                logger.warning(
                    "chat.stream.codex_error",
                    code=evt.error.code,
                    message=evt.error.message,
                )
                finish = "error"
                break

            elif isinstance(evt, TurnCompleted):
                break

            elif isinstance(evt, TurnFailed):
                # H4: TurnFailed is a terminal event — treat as error completion.
                logger.warning(
                    "chat.stream.turn_failed",
                    error=getattr(evt, "error", None),
                )
                finish = "error"
                break
            # All other events (TurnStarted, ItemStarted, etc.): skip silently.

    except asyncio.CancelledError:
        logger.info("chat.stream.client_disconnect", completion_id=cid)
        raise  # propagate so runner cleanup fires

    except Exception:
        logger.exception("chat.stream.unexpected_error", completion_id=cid)
        finish = "error"

    # Ensure a role chunk was sent even if Codex produced no agent_message output.
    if not sent_role:
        yield _make_chunk({"role": "assistant", "content": ""})

    # Final chunk with finish_reason (no content delta — client already has all bytes).
    yield _make_chunk({}, finish_reason=finish)

    # Optional usage-only chunk (researcher-02 §A.3 final-with-usage shape).
    if req.stream_options and req.stream_options.include_usage:
        yield _make_chunk({}, choices_empty=True, completion_tokens=_completion_tokens)

    yield b"data: [DONE]\n\n"
