"""
Streaming handler for POST /v1/responses.

Drives ResponseEmitter → yields SSE bytes with dual ``event:`` + ``data:``
lines per the Responses API wire format (researcher-02 §B.1).

Wire format (different from chat-completions which uses data-only):
    event: <type>\\n
    data: <json>\\n
    \\n

No [DONE] sentinel — socket closes after response.completed / failed / cancelled.

MM1: caller wraps this generator with keepalive_wrap(interval=15.0).
C3:  workspace cleanup registered as BackgroundTask by the route layer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import structlog

from src.codex.events import CodexEvent
from src.responses.events_emitter import ResponseEmitter

logger = structlog.get_logger(__name__)


def _sse_bytes(event_type: str, payload: dict[str, object]) -> bytes:
    """Encode a single Responses API SSE event as bytes.

    Format: ``event: <type>\\ndata: <json>\\n\\n``
    Both lines MUST be present (researcher-02 §B.1).
    """
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()


async def stream_responses(
    events: AsyncIterator[CodexEvent],
    *,
    emitter: ResponseEmitter,
    request: object,
) -> AsyncIterator[bytes]:
    """Yield SSE bytes for a Responses API streaming request.

    Args:
        events:  Async iterator of Codex events from run_codex().
        emitter: Pre-constructed ResponseEmitter for this request.
        request: Starlette Request (for is_disconnected() polling).

    Yields:
        SSE-encoded bytes: ``b"event: <type>\\ndata: <json>\\n\\n"``.

    Notes:
        - keepalive_wrap should be applied by the caller (MM1).
        - workspace cleanup is caller's responsibility (BackgroundTask).
        - On client disconnect: emits response.cancelled then returns.
        - On ConnectionError / CancelledError: swallows gracefully.
    """
    # Emit response.created immediately.
    for evt_type, payload in emitter.start():
        yield _sse_bytes(evt_type, payload)

    try:
        async for codex_evt in events:
            # Poll disconnect between each event (low-overhead).
            if hasattr(request, "is_disconnected") and await request.is_disconnected():
                logger.info(
                    "responses.stream.client_disconnected",
                    response_id=emitter.response_id,
                )
                for evt_type, payload in emitter.cancel():
                    yield _sse_bytes(evt_type, payload)
                return

            for evt_type, payload in emitter.on_codex_event(codex_evt):
                yield _sse_bytes(evt_type, payload)

    except (asyncio.CancelledError, GeneratorExit):
        # Client gone or outer task cancelled — emit cancel event best-effort.
        logger.info(
            "responses.stream.cancelled",
            response_id=emitter.response_id,
        )
        for evt_type, payload in emitter.cancel():
            try:  # noqa: SIM105  — contextlib.suppress cannot wrap yield
                yield _sse_bytes(evt_type, payload)
            except Exception:  # noqa: BLE001
                pass
        raise

    except Exception:
        logger.exception(
            "responses.stream.unexpected_error",
            response_id=emitter.response_id,
        )
        # Emit error + failed terminal events before propagating.
        for evt_type, payload in emitter.finalize():
            try:  # noqa: SIM105  — contextlib.suppress cannot wrap yield
                yield _sse_bytes(evt_type, payload)
            except Exception:  # noqa: BLE001
                pass
        return

    # Normal path: emit terminal event (response.completed or response.failed).
    for evt_type, payload in emitter.finalize():
        yield _sse_bytes(evt_type, payload)
