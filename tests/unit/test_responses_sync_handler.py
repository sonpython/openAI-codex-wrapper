"""
Unit tests for responses sync handler (collect_response).

Covers:
  - Happy path: TurnCompleted → status="completed", output populated, usage set
  - ErrorEvent → status="failed", error field populated
  - TurnFailed → status="failed"
  - Empty agent output → status="completed", output=[]
  - Usage falls back to tiktoken estimate when TurnCompleted has no usage
"""

from __future__ import annotations

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
    ThreadStarted,
    TokenUsage,
    TurnCompleted,
    TurnFailed,
)
from src.responses.sync_handler import collect_response

_RESPONSE_ID = "resp_aabbccddeeff001122334"
_MODEL = "codex-cli"
_CREATED_AT = "2026-04-27T10:00:00Z"
_PROMPT = "User:\nhello\n\nAssistant:\n"


async def _events(*evts: CodexEvent) -> AsyncIterator[CodexEvent]:
    for e in evts:
        yield e


# ── Happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_status_completed() -> None:
    item = ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id="i1", text="pong"),
    )
    resp = await collect_response(
        _events(item, TurnCompleted(type="turn.completed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.status == "completed"
    assert resp.id == _RESPONSE_ID
    assert resp.model == _MODEL


@pytest.mark.asyncio
async def test_happy_path_output_text_populated() -> None:
    item = ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id="i1", text="hello there"),
    )
    resp = await collect_response(
        _events(item, TurnCompleted(type="turn.completed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert len(resp.output) == 1
    assert resp.output[0].content[0].text == "hello there"
    assert resp.output[0].role == "assistant"
    assert resp.output[0].type == "message"


@pytest.mark.asyncio
async def test_happy_path_usage_from_turn_completed() -> None:
    item = ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id="i1", text="hi"),
    )
    turn = TurnCompleted(
        type="turn.completed",
        usage=TokenUsage(input_tokens=10, output_tokens=5, reasoning_tokens=0),
    )
    resp = await collect_response(
        _events(item, turn),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.usage is not None
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 5
    assert resp.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_usage_falls_back_to_estimate_when_no_codex_usage() -> None:
    item = ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id="i1", text="some output text"),
    )
    # TurnCompleted with no usage
    resp = await collect_response(
        _events(item, TurnCompleted(type="turn.completed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.usage is not None
    assert resp.usage.total_tokens > 0


# ── Error paths ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_event_yields_failed_status() -> None:
    err = ErrorEvent(
        type="error",
        error=ErrorPayload(code="EXIT_NONZERO", message="codex exited 1"),
    )
    resp = await collect_response(
        _events(err),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.status == "failed"
    assert resp.error is not None
    assert resp.error.code == "server_error"


@pytest.mark.asyncio
async def test_timeout_error_maps_code() -> None:
    err = ErrorEvent(
        type="error",
        error=ErrorPayload(code="TIMEOUT", message="exceeded 120s"),
    )
    resp = await collect_response(
        _events(err),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.error is not None
    assert resp.error.code == "timeout"


@pytest.mark.asyncio
async def test_turn_failed_yields_failed_status() -> None:
    resp = await collect_response(
        _events(TurnFailed(type="turn.failed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.status == "failed"
    assert resp.error is not None


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_output_returns_completed_with_no_items() -> None:
    resp = await collect_response(
        _events(TurnCompleted(type="turn.completed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.status == "completed"
    assert resp.output == []


@pytest.mark.asyncio
async def test_metadata_propagated() -> None:
    resp = await collect_response(
        _events(TurnCompleted(type="turn.completed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
        metadata={"k": "v"},
    )
    assert resp.metadata == {"k": "v"}


@pytest.mark.asyncio
async def test_multiple_agent_messages_concatenated() -> None:
    """Multiple agent_message items should be joined."""
    items = [
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id=f"i{i}", text=f"part{i}"),
        )
        for i in range(3)
    ]
    resp = await collect_response(
        _events(*items, TurnCompleted(type="turn.completed")),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.status == "completed"
    assert len(resp.output) == 1
    assert "part0" in resp.output[0].content[0].text
    assert "part1" in resp.output[0].content[0].text


@pytest.mark.asyncio
async def test_thread_started_ignored() -> None:
    """Non-agent events should be silently ignored."""
    resp = await collect_response(
        _events(
            ThreadStarted(type="thread.started", thread_id="t1"),
            ItemCompleted(
                type="item.completed",
                item=AgentMessageItem(type="agent_message", id="i1", text="ok"),
            ),
            TurnCompleted(type="turn.completed"),
        ),
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        prompt=_PROMPT,
    )
    assert resp.status == "completed"
    assert resp.output[0].content[0].text == "ok"
