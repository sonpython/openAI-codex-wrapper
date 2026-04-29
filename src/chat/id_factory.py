"""
Completion ID factory.

Generates opaque IDs in the shape ``chatcmpl_<26 hex chars>`` matching
the rough format of OpenAI's IDs (their format is internal/opaque; we
just need prefix + enough entropy to be collision-free per deployment).
"""

from __future__ import annotations

import secrets


def new_completion_id() -> str:
    """Return a new unique completion ID.

    Format: ``chatcmpl_<26 lowercase hex chars>``
    Entropy: 13 bytes = 104 bits — collision-free at any realistic request rate.
    """
    return f"chatcmpl_{secrets.token_hex(13)}"


def make_tool_call_id() -> str:
    """Return a new unique tool call ID.

    Format: ``call_<24 lowercase hex chars>``
    Entropy: 12 bytes = 96 bits — matches OpenAI's call_xxx shape.
    """
    return f"call_{secrets.token_hex(12)}"
