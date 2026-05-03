"""
Synthetic terminal-event helper for responses SSE finalization.

Emits a well-formed ``response.failed`` SSE event so aiohttp / httpx clients
see a clean chunked-transfer EOF when the wrapper catches an exception.

Security: event contains NO stderr tail or internal trace — status only.
"""

from __future__ import annotations

import json


def synth_failed_event(response_id: str) -> bytes:
    """Return serialised SSE bytes for a synthetic ``response.failed`` event.

    Shape matches what ResponseEmitter.finalize() already emits on the error
    path so clients that inspect the event type see a consistent result.

    Args:
        response_id: The ``resp_...`` ID shared with earlier events in the stream.

    Returns:
        ``b"event: response.failed\\ndata: {...}\\n\\n"`` bytes ready to yield.
    """
    payload = {
        "type": "response.failed",
        "response": {
            "id": response_id,
            "object": "response",
            "status": "failed",
        },
    }
    return f"event: response.failed\ndata: {json.dumps(payload)}\n\n".encode()
