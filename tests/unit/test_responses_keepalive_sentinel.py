"""
MM1 sentinel test for Responses API stream: keepalive comments interleave
with real SSE events during long Codex silences.

Uses a slow mock event stream that pauses 0.12s mid-stream with a 0.05s
keepalive interval — asserts at least one b": keepalive\\n\\n" comment appears
between the response.created event and the response.completed event.

Also asserts that real SSE events (with event: lines) are NOT suppressed by
keepalive injection — both keepalive AND data events appear.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.codex.events import (
    AgentMessageItem,
    ItemCompleted,
    TurnCompleted,
)
from src.gateway.sse_helpers import keepalive_wrap
from src.responses.events_emitter import ResponseEmitter
from src.responses.stream_handler import stream_responses

_RESPONSE_ID = "resp_aabbccddeeff001122334"
_MODEL = "codex-cli"
_CREATED_AT = "2026-04-27T10:00:00Z"


class _MockRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _slow_codex_events() -> object:
    """Yield one item, pause 0.12s, yield completion — simulates long Codex turn."""
    yield ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id="i1", text="hello"),
    )
    await asyncio.sleep(0.12)
    yield TurnCompleted(type="turn.completed")


@pytest.mark.asyncio
async def test_keepalive_injected_during_slow_codex_turn() -> None:
    """Keepalive must appear when Codex is silent for > interval seconds."""
    emitter = ResponseEmitter(
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
    )
    raw_stream = stream_responses(
        _slow_codex_events(),  # type: ignore[arg-type]
        emitter=emitter,
        request=_MockRequest(),
    )
    # Use short interval (0.05s) to trigger keepalive during the 0.12s pause
    kept = keepalive_wrap(raw_stream, interval=0.05)

    collected: list[bytes] = []
    async for chunk in kept:
        collected.append(chunk)

    all_bytes = b"".join(collected)

    # Real events must be present
    assert b"event: response.created" in all_bytes
    assert b"event: response.completed" in all_bytes

    # At least one keepalive comment must have been injected
    assert (
        b": keepalive\n\n" in all_bytes
    ), "No keepalive comment found during 0.12s pause with 0.05s interval"


@pytest.mark.asyncio
async def test_keepalive_does_not_suppress_real_events() -> None:
    """Keepalive injection must not drop any real SSE events."""
    emitter = ResponseEmitter(
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
    )

    async def _fast_events() -> object:
        yield ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="fast"),
        )
        yield TurnCompleted(type="turn.completed")

    raw_stream = stream_responses(
        _fast_events(),  # type: ignore[arg-type]
        emitter=emitter,
        request=_MockRequest(),
    )
    # High interval — no keepalive should fire
    kept = keepalive_wrap(raw_stream, interval=5.0)

    collected: list[bytes] = []
    async for chunk in kept:
        collected.append(chunk)

    all_bytes = b"".join(collected)

    # All mandatory events must be present
    assert b"event: response.created" in all_bytes
    assert b"event: response.completed" in all_bytes
    assert b"event: response.output_text.delta" in all_bytes

    # No keepalive should have fired on a fast stream
    assert b": keepalive\n\n" not in all_bytes


@pytest.mark.asyncio
async def test_keepalive_position_between_events() -> None:
    """Keepalive comments must appear BETWEEN real events, not before response.created."""
    emitter = ResponseEmitter(
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
    )
    raw_stream = stream_responses(
        _slow_codex_events(),  # type: ignore[arg-type]
        emitter=emitter,
        request=_MockRequest(),
    )
    kept = keepalive_wrap(raw_stream, interval=0.05)

    collected: list[bytes] = []
    async for chunk in kept:
        collected.append(chunk)

    # Find indices of key events
    created_idx = next((i for i, c in enumerate(collected) if b"response.created" in c), None)
    completed_idx = next((i for i, c in enumerate(collected) if b"response.completed" in c), None)
    keepalive_indices = [i for i, c in enumerate(collected) if c == b": keepalive\n\n"]

    assert created_idx is not None
    assert completed_idx is not None
    assert keepalive_indices, "Expected at least one keepalive"

    # All keepalives must appear AFTER response.created
    for ki in keepalive_indices:
        assert (
            ki > created_idx
        ), f"keepalive at index {ki} appeared before response.created at {created_idx}"
