"""
Shared helpers for the Responses API emitter and route.

Extracted to keep individual files ≤ 200 LOC:
  - chunk_text: whitespace-boundary text chunker for simulating streaming deltas
  - new_item_id / new_event_id: opaque ID generators
  - iso_now: ISO-8601 UTC timestamp string
  - emit_agent_message_events: emits delta/done/completed sequence for one item
  - build_responses_prompt: assembles Codex prompt from ResponsesRequest fields
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from src.codex.events import AgentMessageItem
from src.gateway.schemas.responses_object import OutputItem, OutputTextContent


def chunk_text(text: str, size: int) -> list[str]:
    """Slice *text* into fixed-width char windows of *size* bytes each.

    Returns exact character sub-strings — no whitespace normalisation.
    Concatenating all chunks reconstructs the original string byte-for-byte:
    ``"".join(chunk_text(t, n)) == t`` for any non-empty *t* and *n* > 0.

    Codex emits agent_message only on item.completed (full text at once). This
    function chunks the text to simulate streaming deltas — not token-accurate.
    Phase-08 may improve granularity via tiktoken.
    """
    if size <= 0 or not text:
        return [text] if text else []
    return [text[i : i + size] for i in range(0, len(text), size)]


def new_item_id() -> str:
    """Generate an opaque ``item_<20 hex>`` ID."""
    return f"item_{secrets.token_hex(10)}"


def new_event_id() -> str:
    """Generate an opaque ``evt_<20 hex>`` ID."""
    return f"evt_{secrets.token_hex(10)}"


def iso_now() -> str:
    """Return current UTC time as ISO-8601 string, e.g. ``2026-04-27T10:30:00Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# EventTuple = (event_type_str, payload_dict)
EventTuple = tuple[str, dict[str, Any]]
EmitFn = Callable[[str, dict[str, Any]], EventTuple]


def emit_agent_message_events(
    item: AgentMessageItem,
    *,
    emit: EmitFn,
    chunk_size: int,
    current_item_id: str | None,
    output_index: int,
    output_items: list[OutputItem],
) -> Iterator[EventTuple]:
    """Emit delta/done/part-done/item-done events for one agent_message item.

    Mutates ``output_items`` by appending the completed OutputItem.

    Args:
        item:            The completed AgentMessageItem from Codex.
        emit:            Bound ``_emit`` method from the enclosing ResponseEmitter.
        chunk_size:      Target chunk size for text deltas.
        current_item_id: Fallback item ID if item.id is empty.
        output_index:    Monotonic index of this output item in the response.
        output_items:    Mutable list to append the completed OutputItem to.
    """
    full_text = item.text
    item_id = item.id or current_item_id or new_item_id()

    for chunk in chunk_text(full_text, chunk_size):
        yield emit(
            "response.output_text.delta",
            {"output_index": output_index, "content_index": 0, "item_id": item_id, "delta": chunk},
        )

    yield emit(
        "response.output_text.done",
        {"output_index": output_index, "content_index": 0, "item_id": item_id, "text": full_text},
    )
    yield emit(
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
        status="completed",
        role="assistant",
        content=[OutputTextContent(type="output_text", text=full_text)],
    )
    output_items.append(completed)
    yield emit(
        "response.output_item.done",
        {"output_index": output_index, "item": completed.model_dump(exclude_none=True)},
    )


def build_responses_prompt(
    input_value: str | list[Any],
    instructions: str | None,
    max_chars: int,
) -> str:
    """Assemble a Codex prompt from ResponsesRequest.input + instructions.

    Args:
        input_value:  str or list[InputItem] from the request.
        instructions: Optional system prompt prepended before input.
        max_chars:    Maximum allowed character length (raises ValueError if exceeded).

    Returns:
        Formatted prompt string ending with ``\\n\\nAssistant:\\n``.

    Raises:
        ValueError: If assembled prompt exceeds max_chars.
    """
    parts: list[str] = []
    if instructions:
        parts.append(f"System:\n{instructions}")

    if isinstance(input_value, str):
        parts.append(f"User:\n{input_value}")
    else:
        for item in input_value:
            content = item.content
            if isinstance(content, str):
                text = content
            else:
                text = "".join(p.text for p in content if hasattr(p, "text"))
            parts.append(f"{item.role.capitalize()}:\n{text}")

    prompt = "\n\n".join(parts) + "\n\nAssistant:\n"

    if len(prompt) > max_chars:
        raise ValueError(
            f"input exceeds maximum length of {max_chars} characters (got {len(prompt)})"
        )
    return prompt
