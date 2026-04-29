"""
Unit tests for the responses stream handler.

Validates:
  - Full state-machine event order as SSE bytes
  - Every SSE chunk has both event: and data: lines
  - No [DONE] sentinel in output
  - Client disconnect path emits response.cancelled
  - Exception in runner causes finalize() terminal event
  - Sequence numbers monotonic across entire stream
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.codex.events import (
    AgentMessageItem,
    CodexEvent,
    ErrorEvent,
    ErrorPayload,
    ItemCompleted,
    ItemStarted,
    ThreadStarted,
    TurnCompleted,
)
from src.responses.events_emitter import ResponseEmitter
from src.responses.stream_handler import stream_responses

_RESPONSE_ID = "resp_aabbccddeeff001122334"
_MODEL = "codex-cli"
_CREATED_AT = "2026-04-27T10:00:00Z"


def _make_emitter() -> ResponseEmitter:
    return ResponseEmitter(
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
    )


async def _events(*evts: CodexEvent) -> AsyncIterator[CodexEvent]:
    for e in evts:
        yield e


class _MockRequest:
    """Minimal request stub — never disconnects."""

    async def is_disconnected(self) -> bool:
        return False


class _DisconnectRequest:
    """Request that reports disconnected after first poll."""

    def __init__(self) -> None:
        self._calls = 0

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > 1


def _parse_sse_chunks(raw_bytes: bytes) -> list[dict]:
    """Parse raw SSE bytes into list of {event_type, payload} dicts."""
    results = []
    events = raw_bytes.decode().split("\n\n")
    for block in events:
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        event_type = None
        data_str = None
        for line in lines:
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data_str = line[6:]
        if event_type and data_str:
            results.append({"event_type": event_type, "payload": json.loads(data_str)})
    return results


# ── Full state machine ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_stream_event_order() -> None:
    codex_events = _events(
        ThreadStarted(type="thread.started", thread_id="t1"),
        ItemStarted(
            type="item.started",
            item=AgentMessageItem(type="agent_message", id="i1", text=""),
        ),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="Hello world"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    chunks: list[bytes] = []
    async for chunk in stream_responses(
        codex_events, emitter=_make_emitter(), request=_MockRequest()
    ):
        chunks.append(chunk)

    raw = b"".join(chunks)
    parsed = _parse_sse_chunks(raw)
    types = [p["event_type"] for p in parsed]

    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert types[2] == "response.output_item.added"
    assert types[3] == "response.content_part.added"
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert "response.content_part.done" in types
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"


@pytest.mark.asyncio
async def test_all_chunks_have_both_event_and_data_lines() -> None:
    codex_events = _events(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hi"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    chunks: list[bytes] = []
    async for chunk in stream_responses(
        codex_events, emitter=_make_emitter(), request=_MockRequest()
    ):
        # keepalive bytes are plain b": keepalive\n\n" — skip those
        if chunk.startswith(b":"):
            continue
        chunks.append(chunk)

    for chunk in chunks:
        text = chunk.decode()
        lines = text.rstrip("\n").split("\n")
        assert any(ln.startswith("event: ") for ln in lines), f"No event: line in {text!r}"
        assert any(ln.startswith("data: ") for ln in lines), f"No data: line in {text!r}"


@pytest.mark.asyncio
async def test_no_done_sentinel_in_stream() -> None:
    codex_events = _events(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="done"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    all_bytes = b""
    async for chunk in stream_responses(
        codex_events, emitter=_make_emitter(), request=_MockRequest()
    ):
        all_bytes += chunk

    assert b"[DONE]" not in all_bytes


@pytest.mark.asyncio
async def test_sequence_numbers_monotonic_across_stream() -> None:
    codex_events = _events(
        ThreadStarted(type="thread.started", thread_id="t1"),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hello world from codex"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    chunks: list[bytes] = []
    async for chunk in stream_responses(
        codex_events, emitter=_make_emitter(), request=_MockRequest()
    ):
        chunks.append(chunk)

    raw = b"".join(chunks)
    parsed = _parse_sse_chunks(raw)
    seq_nums = [p["payload"]["sequence_number"] for p in parsed]
    assert seq_nums == list(range(len(seq_nums))), f"Gaps in sequence: {seq_nums}"


# ── First event bytes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_chunk_starts_with_response_created_prefix() -> None:
    codex_events = _events(TurnCompleted(type="turn.completed"))
    gen = stream_responses(codex_events, emitter=_make_emitter(), request=_MockRequest())
    first_chunk = await gen.__anext__()
    assert first_chunk.startswith(
        b"event: response.created\ndata: {"
    ), f"First chunk does not match spec: {first_chunk[:60]!r}"


# ── Client disconnect ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_disconnect_emits_response_cancelled() -> None:
    async def _slow_events() -> AsyncIterator[CodexEvent]:
        for i in range(5):
            yield ItemCompleted(
                type="item.completed",
                item=AgentMessageItem(type="agent_message", id=f"i{i}", text=f"chunk{i}"),
            )

    chunks: list[bytes] = []
    async for chunk in stream_responses(
        _slow_events(), emitter=_make_emitter(), request=_DisconnectRequest()
    ):
        chunks.append(chunk)

    raw = b"".join(chunks)
    assert b"response.cancelled" in raw


# ── Error event in stream ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_event_emitted_followed_by_response_failed() -> None:
    codex_events = _events(
        ErrorEvent(type="error", error=ErrorPayload(code="EXIT_NONZERO", message="oops")),
    )
    chunks: list[bytes] = []
    async for chunk in stream_responses(
        codex_events, emitter=_make_emitter(), request=_MockRequest()
    ):
        chunks.append(chunk)

    raw = b"".join(chunks)
    assert b'"error"' in raw
    assert b"response.failed" in raw


# ── Payload type matches event: line ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_payload_type_matches_event_line_in_stream() -> None:
    codex_events = _events(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="test"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    chunks: list[bytes] = []
    async for chunk in stream_responses(
        codex_events, emitter=_make_emitter(), request=_MockRequest()
    ):
        if chunk.startswith(b":"):
            continue
        chunks.append(chunk)

    parsed = _parse_sse_chunks(b"".join(chunks))
    for item in parsed:
        assert (
            item["payload"]["type"] == item["event_type"]
        ), f"event: {item['event_type']!r} != payload.type {item['payload']['type']!r}"
