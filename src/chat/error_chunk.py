"""
Synthetic terminal-chunk helper for chat completions SSE finalization.

Emits a well-formed ChatCompletionChunk with finish_reason="error" and an
empty delta so aiohttp / httpx clients see a clean chunked-transfer EOF.

Security: chunk contains NO stderr tail or internal trace — finish_reason only.
"""

from __future__ import annotations

import json


def synth_error_chunk(model: str, cid: str, created: int) -> bytes:
    """Return a serialised SSE ``data:`` line with finish_reason='error'.

    Shape is identical to what stream_chunks already emits on ErrorEvent so
    clients that parse finish_reason see a consistent result.

    Args:
        model:   Model name string to echo back (e.g. "codex-cli").
        cid:     Completion ID (``chatcmpl_...``) shared with earlier chunks.
        created: Unix timestamp integer from the start of the request.

    Returns:
        ``b"data: {...}\\n\\n"`` bytes ready to yield from the SSE generator.
    """
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "error",
                "logprobs": None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()
