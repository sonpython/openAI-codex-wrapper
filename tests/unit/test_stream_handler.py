"""
Unit tests for src/chat/stream_handler.py.

Verifies exact SSE byte-sequence properties using mocked event iterators.

Covers:
  - First chunk has delta.role="assistant" + delta.content
  - Middle chunks have delta.content only
  - Final chunk has finish_reason="stop", delta={}
  - data: [DONE] present as last line
  - include_usage=True produces extra choices=[] chunk before [DONE]
  - ErrorEvent → finish_reason="error" then [DONE]
  - Empty agent_message output still emits role chunk + final + [DONE]
  - CancelledError propagates (client disconnect)
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.codex.events import (
    AgentMessageItem,
    ErrorEvent,
    ErrorPayload,
    ItemCompleted,
    TurnCompleted,
    TurnFailed,
)
from src.gateway.schemas.chat_request import ChatCompletionRequest


def _req(**kwargs: object) -> ChatCompletionRequest:
    base = {"model": "codex-cli", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kwargs)  # type: ignore[arg-type]
    return ChatCompletionRequest(**base)  # type: ignore[arg-type]


async def _collect(req: ChatCompletionRequest, *events: object) -> list[bytes]:
    from src.chat.stream_handler import stream_chunks

    async def _iter() -> AsyncIterator[object]:
        for e in events:
            yield e

    chunks: list[bytes] = []
    async for chunk in stream_chunks(req, "User:\nhi\n\nAssistant:\n", _iter()):  # type: ignore[arg-type]
        chunks.append(chunk)
    return chunks


def _parse_data_chunks(chunks: list[bytes]) -> list[dict]:
    """Parse SSE data lines (excluding [DONE]) into dicts."""
    result = []
    for chunk in chunks:
        text = chunk.decode()
        for line in text.strip().split("\n\n"):
            line = line.strip()
            if line.startswith("data: ") and "[DONE]" not in line:
                result.append(json.loads(line[6:]))
    return result


def _has_done(chunks: list[bytes]) -> bool:
    combined = b"".join(chunks)
    return b"data: [DONE]" in combined


@pytest.mark.asyncio
async def test_first_chunk_has_role_and_content() -> None:
    chunks = await _collect(
        _req(),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="Hello"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    first = parsed[0]
    assert first["choices"][0]["delta"].get("role") == "assistant"
    assert first["choices"][0]["delta"].get("content") == "Hello"
    # finish_reason=None is excluded from JSON by exclude_none=True; .get() returns None
    assert first["choices"][0].get("finish_reason") is None


@pytest.mark.asyncio
async def test_middle_chunks_have_content_only() -> None:
    chunks = await _collect(
        _req(),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="First"),
        ),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i2", text="Second"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    # second content chunk (index 1) should have no role
    second = parsed[1]
    assert (
        "role" not in second["choices"][0]["delta"] or second["choices"][0]["delta"]["role"] is None
    )
    assert second["choices"][0]["delta"].get("content") == "Second"


@pytest.mark.asyncio
async def test_final_chunk_has_finish_reason_stop() -> None:
    chunks = await _collect(
        _req(),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="text"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    final = parsed[-1]
    assert final["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_done_terminator_present() -> None:
    chunks = await _collect(
        _req(),
        TurnCompleted(type="turn.completed"),
    )
    assert _has_done(chunks)


@pytest.mark.asyncio
async def test_include_usage_produces_extra_choices_empty_chunk() -> None:
    req = _req(stream=True, stream_options={"include_usage": True})
    chunks = await _collect(
        req,
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hi"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    # Second-to-last parsed chunk (before [DONE]) should have choices=[] + usage
    usage_chunk = parsed[-1]
    assert usage_chunk["choices"] == []
    assert "usage" in usage_chunk
    assert usage_chunk["usage"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_error_event_sets_finish_reason_error() -> None:
    chunks = await _collect(
        _req(),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="partial"),
        ),
        ErrorEvent(
            type="error",
            error=ErrorPayload(code="ERR", message="boom"),
        ),
    )
    parsed = _parse_data_chunks(chunks)
    final = parsed[-1]
    assert final["choices"][0]["finish_reason"] == "error"
    assert _has_done(chunks)


@pytest.mark.asyncio
async def test_empty_output_still_emits_role_chunk_and_done() -> None:
    chunks = await _collect(
        _req(),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    # Must have at least role chunk + final chunk
    assert len(parsed) >= 2
    role_chunk = parsed[0]
    assert role_chunk["choices"][0]["delta"].get("role") == "assistant"
    assert _has_done(chunks)


@pytest.mark.asyncio
async def test_cancelled_error_propagates() -> None:
    from src.chat.stream_handler import stream_chunks

    async def _cancelling_iter() -> AsyncIterator[object]:
        yield ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hi"),
        )
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async for _ in stream_chunks(_req(), "prompt", _cancelling_iter()):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_all_chunks_are_valid_sse_format() -> None:
    chunks = await _collect(
        _req(),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hello"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    for chunk in chunks:
        text = chunk.decode()
        assert text.startswith("data: "), f"unexpected chunk format: {text!r}"
        assert text.endswith("\n\n"), f"chunk missing trailing newlines: {text!r}"


# ── C2: max_tokens cap tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_tokens_cap_finish_reason_is_length() -> None:
    """When max_tokens is hit, finish_reason must be 'length'."""
    chunks = await _collect(
        _req(max_tokens=1),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hello world"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    final = parsed[-1]
    assert final["choices"][0]["finish_reason"] == "length"
    assert _has_done(chunks)


@pytest.mark.asyncio
async def test_max_tokens_cap_no_correction_chunk() -> None:
    """C2 fix: NO extra correction delta emitted after cap — client gets exactly
    what was streamed (content chunk + final finish chunk), not content + correction + final.
    """
    chunks = await _collect(
        _req(max_tokens=1),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="hello world"),
        ),
        TurnCompleted(type="turn.completed"),
    )
    parsed = _parse_data_chunks(chunks)
    # Sequence must be: [role+content chunk, final chunk with finish_reason]
    # No extra "correction" delta chunk between them.
    content_chunks = [
        c
        for c in parsed
        if c.get("choices") and c["choices"][0]["delta"].get("content") is not None
    ]
    final_chunks = [
        c for c in parsed if c.get("choices") and c["choices"][0].get("finish_reason") is not None
    ]
    # Exactly one content-bearing chunk (the first one sent before cap), then final.
    assert len(content_chunks) == 1
    assert len(final_chunks) == 1
    assert final_chunks[0]["choices"][0]["finish_reason"] == "length"


# ── H4: TurnFailed event tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_failed_sets_finish_reason_error() -> None:
    """H4: TurnFailed must produce finish_reason='error' then [DONE]."""
    chunks = await _collect(
        _req(),
        TurnFailed(type="turn.failed", error={"code": "model_error", "message": "turn failed"}),
    )
    parsed = _parse_data_chunks(chunks)
    # Role chunk (empty content since no agent message) + final chunk
    final = parsed[-1]
    assert final["choices"][0]["finish_reason"] == "error"
    assert _has_done(chunks)


@pytest.mark.asyncio
async def test_turn_failed_after_partial_content() -> None:
    """H4: TurnFailed after some content: still finish_reason='error'."""
    chunks = await _collect(
        _req(),
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="partial"),
        ),
        TurnFailed(type="turn.failed"),
    )
    parsed = _parse_data_chunks(chunks)
    final = parsed[-1]
    assert final["choices"][0]["finish_reason"] == "error"


# ── H5: O(N) tokenization tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_estimate_tokens_called_linearly() -> None:
    """H5: _count_tokens must be called O(N) times for N chunks, not O(N²).

    We patch _count_tokens in stream_handler and count invocations.
    For N content chunks, _count_tokens should be called at most N + C times
    (N for the pieces + a small constant for prompt in _build_usage).
    """
    import src.chat.stream_handler as sh_mod  # noqa: PLC0415

    n_chunks = 5
    call_count = 0
    real_count_tokens = sh_mod._count_tokens

    def counting_count_tokens(text: str) -> int:
        nonlocal call_count
        call_count += 1
        return real_count_tokens(text)

    events = [
        ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id=f"i{i}", text=f"word{i}"),
        )
        for i in range(n_chunks)
    ] + [TurnCompleted(type="turn.completed")]

    with patch.object(sh_mod, "_count_tokens", side_effect=counting_count_tokens):
        # include_usage=True so the usage chunk triggers _build_usage → _count_tokens(prompt)
        await _collect(_req(stream=True, stream_options={"include_usage": True}), *events)

    # O(N): N calls for chunk pieces + 1 call for prompt in _build_usage = N+1 total.
    # O(N²) would be ~N*(N+1)/2 = 15 calls for N=5.
    assert (
        call_count <= n_chunks + 2
    ), f"Expected O(N) calls (≤{n_chunks + 2}), got {call_count} — possible O(N²) regression"
