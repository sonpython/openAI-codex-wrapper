"""
Unit tests for src/chat/sync_handler.py.

All tests use a mocked async iterator — no real codex subprocess.

Covers:
  - Happy path: agent_message events collected → ChatCompletion
  - ErrorEvent mid-stream → finish_reason="error"
  - TurnCompleted stops iteration
  - max_tokens truncation → finish_reason="length"
  - Unexpected exception propagates
  - Usage tokens > 0
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.codex.events import (
    AgentMessageItem,
    ErrorEvent,
    ErrorPayload,
    ItemCompleted,
    ThreadStarted,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
)
from src.gateway.schemas.chat_request import ChatCompletionRequest


def _req(**kwargs: object) -> ChatCompletionRequest:
    base = {"model": "codex-cli", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kwargs)  # type: ignore[arg-type]
    return ChatCompletionRequest(**base)  # type: ignore[arg-type]


async def _iter(*events: object) -> AsyncIterator[object]:
    for evt in events:
        yield evt


@pytest.mark.asyncio
async def test_happy_path_collects_agent_messages() -> None:
    from src.chat.sync_handler import handle_sync

    events = _iter(
        ThreadStarted(type="thread.started", thread_id="t1"),
        TurnStarted(type="turn.started"),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hello "),
        ),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i2", text="world"),
        ),
        TurnCompleted(type="turn.completed"),
    )

    result = await handle_sync(_req(), "User:\nhi\n\nAssistant:\n", events)  # type: ignore[arg-type]

    assert result.choices[0].message.content == "hello world"
    assert result.choices[0].finish_reason == "stop"
    assert result.object == "chat.completion"
    assert result.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_error_event_sets_finish_reason_error() -> None:
    from src.chat.sync_handler import handle_sync

    events = _iter(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="partial"),
        ),
        ErrorEvent(
            type="error",
            error=ErrorPayload(code="SOME_ERR", message="something failed"),
        ),
    )

    result = await handle_sync(_req(), "prompt", events)  # type: ignore[arg-type]

    assert result.choices[0].finish_reason == "error"
    assert result.choices[0].message.content == "partial"


@pytest.mark.asyncio
async def test_turn_completed_stops_iteration() -> None:
    from src.chat.sync_handler import handle_sync

    # TurnCompleted appears before a second AgentMessageItem — must not collect after.
    events = _iter(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="first"),
        ),
        TurnCompleted(type="turn.completed"),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i2", text="SHOULD NOT APPEAR"),
        ),
    )

    result = await handle_sync(_req(), "prompt", events)  # type: ignore[arg-type]

    assert result.choices[0].message.content == "first"
    assert "SHOULD NOT APPEAR" not in result.choices[0].message.content


@pytest.mark.asyncio
async def test_max_tokens_truncation_sets_length_finish() -> None:
    from src.chat.sync_handler import handle_sync

    # 1 max_token will definitely truncate any non-trivial text
    events = _iter(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(
                type="agent_message",
                id="i1",
                text="This is a long response that exceeds one token",
            ),
        ),
        TurnCompleted(type="turn.completed"),
    )

    result = await handle_sync(_req(max_tokens=1), "prompt", events)  # type: ignore[arg-type]

    assert result.choices[0].finish_reason == "length"
    # content must be shorter than the original
    assert len(result.choices[0].message.content) < len(
        "This is a long response that exceeds one token"
    )


@pytest.mark.asyncio
async def test_no_agent_message_returns_empty_stop() -> None:
    from src.chat.sync_handler import handle_sync

    events = _iter(TurnCompleted(type="turn.completed"))

    result = await handle_sync(_req(), "prompt", events)  # type: ignore[arg-type]

    assert result.choices[0].message.content == ""
    assert result.choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_unexpected_exception_propagates() -> None:
    from src.chat.sync_handler import handle_sync

    async def _bad_iter() -> AsyncIterator[object]:
        yield TurnStarted(type="turn.started")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await handle_sync(_req(), "prompt", _bad_iter())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_turn_failed_sets_finish_reason_error() -> None:
    """H4: TurnFailed event must set finish_reason='error' in sync path."""
    from src.chat.sync_handler import handle_sync

    events = _iter(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="partial response"),
        ),
        TurnFailed(type="turn.failed", error={"code": "model_error", "message": "failed"}),
    )

    result = await handle_sync(_req(), "prompt", events)  # type: ignore[arg-type]

    assert result.choices[0].finish_reason == "error"
    # Content collected before TurnFailed is preserved.
    assert result.choices[0].message.content == "partial response"


@pytest.mark.asyncio
async def test_turn_failed_no_content_still_error() -> None:
    """H4: TurnFailed with no prior content → finish_reason='error', content=''."""
    from src.chat.sync_handler import handle_sync

    events = _iter(TurnFailed(type="turn.failed"))

    result = await handle_sync(_req(), "prompt", events)  # type: ignore[arg-type]

    assert result.choices[0].finish_reason == "error"
    assert result.choices[0].message.content == ""


@pytest.mark.asyncio
async def test_usage_tokens_positive() -> None:
    from src.chat.sync_handler import handle_sync

    events = _iter(
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="response text"),
        ),
        TurnCompleted(type="turn.completed"),
    )

    result = await handle_sync(_req(), "User:\nhi\n\nAssistant:\n", events)  # type: ignore[arg-type]

    assert result.usage.prompt_tokens > 0
    assert result.usage.completion_tokens > 0
    assert result.usage.total_tokens == (
        result.usage.prompt_tokens + result.usage.completion_tokens
    )
