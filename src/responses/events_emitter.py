"""
ResponseEmitter: maps Codex events → OpenAI Responses API events.

State machine with monotonic sequence_number. One instance per request.
Lifecycle: response.created → in_progress → output_item.added →
content_part.added → output_text.delta×N → output_text.done →
content_part.done → output_item.done → response.completed.
Error path: error → response.failed. Cancel path: response.cancelled.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import structlog

from src.codex.events import (
    AgentMessageItem,
    CodexEvent,
    ErrorEvent,
    ItemCompleted,
    ItemStarted,
    ThreadStarted,
    TurnCompleted,
    TurnFailed,
)
from src.gateway.schemas.responses_object import (
    OutputItem,
    OutputTokensDetails,
    ResponseError,
    ResponseObject,
    ResponseUsage,
)
from src.responses.responses_helpers import (
    chunk_text,
    emit_agent_message_events,
    new_event_id,
    new_item_id,
)
from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Public alias so tests can import _chunk_text from this module.
_chunk_text = chunk_text

EventTuple = tuple[str, dict[str, Any]]


class ResponseEmitter:
    """Stateful emitter: Codex events → Responses API events. One instance per request."""

    def __init__(
        self, response_id: str, model: str, created_at: str, metadata: dict[str, str] | None = None
    ) -> None:
        self.response_id = response_id
        self.model = model
        self.created_at = created_at
        self.metadata = metadata or {}
        self._seq: int = 0
        self._in_progress_sent: bool = False
        self._current_item_id: str | None = None
        # Tracks which output slot the *currently open* item occupies.
        self._current_output_index: int = 0
        # Incremented each time an output_item.done is emitted.
        self._next_output_index: int = 0
        # True when output_item.added has been emitted but output_item.done hasn't.
        self._item_open: bool = False
        self._output_items: list[OutputItem] = []
        self._usage: ResponseUsage | None = None
        self._cancelled: bool = False
        self._failed: bool = False
        self._error: ResponseError | None = None

    def _emit(self, event_type: str, payload: dict[str, Any]) -> EventTuple:
        payload["event_id"] = new_event_id()
        payload["type"] = event_type
        payload["sequence_number"] = self._seq
        self._seq += 1
        return event_type, payload

    def _snapshot(
        self,
        status: str,
        output: list[OutputItem] | None = None,
        usage: ResponseUsage | None = None,
        error: ResponseError | None = None,
    ) -> dict[str, Any]:
        obj = ResponseObject(
            id=self.response_id,
            created_at=self.created_at,
            status=status,  # type: ignore[arg-type]
            model=self.model,
            output=output or [],
            usage=usage,
            metadata=self.metadata or None,
            error=error,
        )
        return obj.model_dump(exclude_none=True)

    def start(self) -> Iterator[EventTuple]:
        """Yield response.created immediately on request entry."""
        yield self._emit("response.created", {"response": self._snapshot("in_progress")})

    def on_codex_event(self, evt: CodexEvent) -> Iterator[EventTuple]:  # noqa: C901
        """Map one Codex event to 0..N Responses API events."""
        chunk_size = get_settings().responses_chunk_chars

        if isinstance(evt, ThreadStarted) and not self._in_progress_sent:
            self._in_progress_sent = True
            yield self._emit("response.in_progress", {"response": self._snapshot("in_progress")})

        elif isinstance(evt, ItemStarted) and isinstance(evt.item, AgentMessageItem):
            # Close any previously open item that never got its .done events
            # (e.g. two consecutive ItemStarted without ItemCompleted in between).
            if self._item_open and self._current_item_id is not None:
                yield from self._close_open_item(full_text="")
            self._current_item_id = evt.item.id
            self._current_output_index = self._next_output_index
            self._item_open = True
            yield self._emit(
                "response.output_item.added",
                {
                    "output_index": self._current_output_index,
                    "item": {
                        "id": self._current_item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            )
            yield self._emit(
                "response.content_part.added",
                {
                    "output_index": self._current_output_index,
                    "content_index": 0,
                    "item_id": self._current_item_id,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            )

        elif isinstance(evt, ItemCompleted) and isinstance(evt.item, AgentMessageItem):
            # Real Codex 0.125 skips ItemStarted for agent_message and emits only
            # ItemCompleted. Lazy-emit the .added events here so every .done has
            # a matching .added per OpenAI Responses API contract.
            if not self._item_open:
                self._current_item_id = evt.item.id or self._current_item_id
                self._current_output_index = self._next_output_index
                self._item_open = True
                yield self._emit(
                    "response.output_item.added",
                    {
                        "output_index": self._current_output_index,
                        "item": {
                            "id": self._current_item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
                yield self._emit(
                    "response.content_part.added",
                    {
                        "output_index": self._current_output_index,
                        "content_index": 0,
                        "item_id": self._current_item_id,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            yield from emit_agent_message_events(
                evt.item,
                emit=self._emit,
                chunk_size=chunk_size,
                current_item_id=self._current_item_id,
                output_index=self._current_output_index,
                output_items=self._output_items,
            )
            self._item_open = False
            self._next_output_index += 1

        elif isinstance(evt, TurnCompleted) and evt.usage:
            self._usage = ResponseUsage(
                input_tokens=evt.usage.input_tokens,
                output_tokens=evt.usage.output_tokens,
                total_tokens=evt.usage.input_tokens + evt.usage.output_tokens,
                output_tokens_details=OutputTokensDetails(
                    reasoning_tokens=evt.usage.reasoning_tokens
                ),
            )

        elif isinstance(evt, TurnFailed):
            self._failed = True
            err_msg = str(evt.error) if evt.error else "turn failed"
            self._error = ResponseError(code="server_error", message=err_msg)
            logger.warning("responses.emitter.turn_failed", error=evt.error)
            yield self._emit("error", {"code": "server_error", "message": err_msg, "param": None})

        elif isinstance(evt, ErrorEvent):
            self._failed = True
            openai_code = "timeout" if evt.error.code == "TIMEOUT" else "server_error"
            self._error = ResponseError(code=openai_code, message=evt.error.message)
            logger.warning(
                "responses.emitter.codex_error",
                code=evt.error.code,
                message=evt.error.message,
            )
            yield self._emit(
                "error", {"code": openai_code, "message": evt.error.message, "param": None}
            )

        else:
            # Reasoning items and unknown types: log + skip (defer to phase-08).
            logger.debug(
                "responses.emitter.skipped_event",
                event_type=getattr(evt, "type", type(evt).__name__),
            )

    def _close_open_item(self, full_text: str = "") -> Iterator[EventTuple]:
        """Emit content_part.done + output_item.done for the currently open item.

        Used both when a new item preempts an existing open one and when
        cancel() needs to flush a partial in-progress item so every .added
        event has a matching .done.
        """
        item_id = self._current_item_id or new_item_id()
        output_index = self._current_output_index
        yield self._emit(
            "response.output_text.done",
            {
                "output_index": output_index,
                "content_index": 0,
                "item_id": item_id,
                "text": full_text,
            },
        )
        yield self._emit(
            "response.content_part.done",
            {
                "output_index": output_index,
                "content_index": 0,
                "item_id": item_id,
                "part": {"type": "output_text", "text": full_text, "annotations": []},
            },
        )
        completed = OutputItem(
            id=item_id,
            type="message",
            status="incomplete",
            role="assistant",
            content=[],
        )
        self._output_items.append(completed)
        yield self._emit(
            "response.output_item.done",
            {"output_index": output_index, "item": completed.model_dump(exclude_none=True)},
        )
        self._item_open = False
        self._next_output_index += 1

    def finalize(self) -> Iterator[EventTuple]:
        """Emit terminal event: response.completed or response.failed."""
        if self._cancelled:
            return
        if self._failed:
            yield self._emit(
                "response.failed",
                {
                    "response": self._snapshot(
                        "failed", output=self._output_items, error=self._error
                    )
                },
            )
            return
        yield self._emit(
            "response.completed",
            {"response": self._snapshot("completed", output=self._output_items, usage=self._usage)},
        )

    def cancel(self) -> Iterator[EventTuple]:
        """Emit response.cancelled (best-effort on disconnect). Idempotent.

        If an output_item is currently open (added but not done), emit
        content_part.done + output_item.done(status=incomplete) first so that
        every .added event has a matching .done before the terminal event.
        """
        if self._cancelled:
            return
        self._cancelled = True
        if self._item_open and self._current_item_id is not None:
            yield from self._close_open_item(full_text="")
        yield self._emit(
            "response.cancelled",
            {"response": self._snapshot("cancelled", output=self._output_items)},
        )
