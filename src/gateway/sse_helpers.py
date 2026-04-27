"""
Shared SSE keepalive utility (addresses Red Team MM1).

``keepalive_wrap`` is an async generator adapter consumed by phases 03, 04,
and 05 — centralised here so the heartbeat pattern is implemented exactly once
(DRY).

How it works
------------
The wrapper awaits the next chunk from the upstream async-iterator with a
``asyncio.wait_for`` timeout of ``interval`` seconds.  If the timeout fires
before a chunk arrives, a SSE comment line ``: keepalive\\n\\n`` is yielded
instead.  SSE clients ignore comment lines, but the bytes keep the TCP
connection alive and reset CDN/proxy idle timers.

Cadence: 15 s (well under typical Caddy/AWS-ALB/Cloudflare 30-60 s idle
defaults).  The keepalive is NOT emitted during normal traffic — only when
the upstream is silent for ``interval`` seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

_KEEPALIVE_BYTES = b": keepalive\n\n"


async def keepalive_wrap(
    upstream: AsyncIterator[bytes],
    interval: float = 15.0,
) -> AsyncIterator[bytes]:
    """Yield bytes from ``upstream``; inject SSE keepalive comments on silence.

    Args:
        upstream: Any async iterator that yields ``bytes`` (SSE chunks).
        interval: Seconds to wait for the next chunk before emitting a
                  keepalive comment.  Default 15 s.

    Yields:
        ``bytes`` — either a chunk from ``upstream`` or ``b": keepalive\\n\\n"``.
    """
    agen = upstream.__aiter__()
    # Shield the __anext__() task so that a timeout does NOT cancel/close the
    # upstream async generator.  Without shield, asyncio.wait_for cancels the
    # coroutine on timeout, which throws CancelledError into the generator and
    # permanently closes it — subsequent __anext__() calls raise StopAsyncIteration
    # instead of resuming after the yield point.  Shield keeps the task alive;
    # we re-await it on the next loop iteration until it eventually resolves.
    #
    # Resource safety: if the outer consumer is GC'd or cancelled, the `pending`
    # future must be explicitly cancelled-and-awaited; otherwise the underlying
    # coroutine leaks as an orphan task.
    pending: asyncio.Future[bytes] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(agen.__anext__())
            try:
                chunk = await asyncio.wait_for(asyncio.shield(pending), timeout=interval)
                # Future resolved — create a fresh one for the next iteration.
                pending = None
                yield chunk
            except TimeoutError:
                yield _KEEPALIVE_BYTES
            except StopAsyncIteration:
                return
    finally:
        # Cancel and drain the in-flight __anext__ task to prevent task/coroutine leak.
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, GeneratorExit):
                await pending
