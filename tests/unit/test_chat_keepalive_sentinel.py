"""
MM1 sentinel test: keepalive_wrap injects `: keepalive\n\n` during silence.

Uses a mock event stream that emits one event, then pauses 0.1s (scaled
down from the spec's 30s — we set interval=0.05s in this test), then emits
another event. Asserts the keepalive comment appears between the two data
chunks.

This is the unit-level regression guard for the MM1 pattern. The real-uvicorn
integration test (C3 sentinel) is in tests/integration/.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.gateway.sse_helpers import keepalive_wrap


async def _slow_stream() -> object:
    """Yield a data chunk, pause longer than the keepalive interval, yield another."""
    yield b"data: first\n\n"
    await asyncio.sleep(0.12)  # longer than 0.05s interval
    yield b"data: second\n\n"


@pytest.mark.asyncio
async def test_keepalive_injected_during_silence() -> None:
    collected: list[bytes] = []
    async for chunk in keepalive_wrap(_slow_stream(), interval=0.05):  # type: ignore[arg-type]
        collected.append(chunk)

    assert b"data: first\n\n" in collected
    assert b"data: second\n\n" in collected
    # At least one keepalive must have been injected during the 0.12s gap
    assert b": keepalive\n\n" in collected


@pytest.mark.asyncio
async def test_no_keepalive_when_stream_is_fast() -> None:
    """Fast stream should not trigger keepalive injection."""

    async def _fast_stream() -> object:
        for i in range(3):
            yield f"data: chunk{i}\n\n".encode()

    collected: list[bytes] = []
    async for chunk in keepalive_wrap(_fast_stream(), interval=5.0):  # type: ignore[arg-type]
        collected.append(chunk)

    assert b": keepalive\n\n" not in collected
    assert len(collected) == 3


@pytest.mark.asyncio
async def test_keepalive_position_between_data_chunks() -> None:
    """Keepalive must appear BETWEEN the first and second data chunks."""
    collected: list[bytes] = []
    async for chunk in keepalive_wrap(_slow_stream(), interval=0.05):  # type: ignore[arg-type]
        collected.append(chunk)

    first_idx = collected.index(b"data: first\n\n")
    second_idx = collected.index(b"data: second\n\n")
    keepalive_indices = [i for i, c in enumerate(collected) if c == b": keepalive\n\n"]

    assert keepalive_indices, "no keepalive found"
    # All keepalives must be between first and second data chunks
    for ki in keepalive_indices:
        assert (
            first_idx < ki < second_idx
        ), f"keepalive at {ki} not between first({first_idx}) and second({second_idx})"
