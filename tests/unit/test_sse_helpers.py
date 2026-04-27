"""
Unit tests for src/gateway/sse_helpers.py.

Key test: feeds a slow async iterator (yields only every 30 s simulated via
asyncio.sleep mock) and asserts that keepalive_wrap emits `: keepalive\\n\\n`
at the 15 s boundary without waiting for the upstream to produce a chunk.

We mock asyncio.wait_for to control timeout behaviour deterministically —
no real wall-clock waiting in unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from src.gateway.sse_helpers import keepalive_wrap

_KEEPALIVE = b": keepalive\n\n"


async def _finite_stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """Yield chunks immediately (no delays)."""
    for chunk in chunks:
        yield chunk


async def _slow_then_chunk(delay: float, chunk: bytes) -> AsyncIterator[bytes]:
    """Simulate a slow upstream: sleep then yield one chunk."""
    await asyncio.sleep(delay)
    yield chunk


@pytest.mark.asyncio
async def test_keepalive_wraps_normal_stream() -> None:
    """Normal (fast) upstream: all chunks pass through unchanged."""
    chunks = [b"data: hello\n\n", b"data: world\n\n"]
    result: list[bytes] = []
    async for item in keepalive_wrap(_finite_stream(chunks), interval=15.0):
        result.append(item)
    assert result == chunks


@pytest.mark.asyncio
async def test_keepalive_emits_on_timeout() -> None:
    """When upstream is silent for interval seconds, a keepalive comment is emitted.

    We replace asyncio.wait_for with a mock that raises TimeoutError on the
    first call (simulating 15 s silence) and then returns a real chunk on the
    second call, allowing StopAsyncIteration to terminate the loop.

    Critically: the mock does NOT cancel the passed future (shielded wrapper),
    mirroring real asyncio.wait_for behaviour: it cancels the shield wrapper
    but NOT the underlying pending Future.  The underlying pending Future must
    survive across iterations so the upstream generator is not closed.
    """
    real_chunk = b"data: late\n\n"

    # Build a one-shot async iterator
    async def _one_chunk() -> AsyncIterator[bytes]:
        yield real_chunk

    agen = _one_chunk().__aiter__()
    call_count = 0

    async def fake_wait_for(fut: object, timeout: float) -> bytes:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate timeout WITHOUT cancelling the shielded future — real
            # asyncio.wait_for cancels the *shield wrapper*, not the inner pending.
            # The inner pending Future must remain alive for re-use next iteration.
            raise TimeoutError()
        # Second call: actually await the future (shielded wrapper over pending)
        return await fut  # type: ignore[misc]

    with patch("src.gateway.sse_helpers.asyncio.wait_for", side_effect=fake_wait_for):
        result: list[bytes] = []
        async for item in keepalive_wrap(agen, interval=15.0):
            result.append(item)

    # First item should be the keepalive, second the real chunk
    assert result[0] == _KEEPALIVE
    assert result[1] == real_chunk


@pytest.mark.asyncio
async def test_keepalive_empty_stream() -> None:
    """Empty upstream terminates immediately with no output."""

    async def _empty() -> AsyncIterator[bytes]:
        return
        yield  # make it an async generator

    result: list[bytes] = []
    async for item in keepalive_wrap(_empty(), interval=15.0):
        result.append(item)
    assert result == []


@pytest.mark.asyncio
async def test_keepalive_multiple_timeouts_then_data() -> None:
    """Multiple consecutive keepalives emitted before a real chunk arrives."""
    real_chunk = b"data: finally\n\n"

    async def _delayed() -> AsyncIterator[bytes]:
        yield real_chunk

    agen = _delayed().__aiter__()
    timeout_count = 0

    async def fake_wait_for(fut: object, timeout: float) -> bytes:
        nonlocal timeout_count
        if timeout_count < 3:
            timeout_count += 1
            # Do NOT cancel fut — real asyncio.wait_for only cancels the shield
            # wrapper, not the underlying pending Future.
            raise TimeoutError()
        return await fut  # type: ignore[misc]

    with patch("src.gateway.sse_helpers.asyncio.wait_for", side_effect=fake_wait_for):
        result: list[bytes] = []
        async for item in keepalive_wrap(agen, interval=15.0):
            result.append(item)

    assert result[:3] == [_KEEPALIVE, _KEEPALIVE, _KEEPALIVE]
    assert result[3] == real_chunk


@pytest.mark.asyncio
async def test_keepalive_no_orphan_task_on_cancellation() -> None:
    """Pending future must be cancelled when outer consumer is cancelled.

    Without the finally-block fix, `pending` would be left as an orphan task
    after the generator is GC'd / cancelled.  We verify via asyncio.all_tasks()
    count: after cancellation the count must return to its baseline.
    """

    async def _infinite() -> AsyncIterator[bytes]:
        """Upstream that never yields — simulates a stalled codex process."""
        while True:
            await asyncio.sleep(3600)  # practically infinite
            yield b"never"

    baseline_tasks = len(asyncio.all_tasks())

    async def _consumer() -> None:
        async for _ in keepalive_wrap(_infinite(), interval=0.01):
            break  # consume exactly one keepalive then exit

    # Run consumer as a task and cancel it mid-flight.
    task = asyncio.ensure_future(_consumer())
    # Let the event loop spin so keepalive_wrap enters its wait_for call.
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Give the event loop one tick to process any pending callbacks.
    await asyncio.sleep(0)

    tasks_after = len(asyncio.all_tasks())
    assert (
        tasks_after <= baseline_tasks
    ), f"Orphan tasks detected: baseline={baseline_tasks}, after={tasks_after}"


@pytest.mark.asyncio
async def test_keepalive_default_interval_is_15s() -> None:
    """keepalive_wrap uses 15.0 s interval by default."""
    captured_timeout: list[float] = []

    async def _one() -> AsyncIterator[bytes]:
        yield b"x"

    agen = _one().__aiter__()

    original_wait_for = asyncio.wait_for

    async def capturing_wait_for(coro, timeout):  # type: ignore[no-untyped-def]
        captured_timeout.append(timeout)
        return await original_wait_for(coro, timeout=60)

    with patch("src.gateway.sse_helpers.asyncio.wait_for", side_effect=capturing_wait_for):
        async for _ in keepalive_wrap(agen):  # no interval arg → default
            pass

    assert captured_timeout[0] == 15.0
