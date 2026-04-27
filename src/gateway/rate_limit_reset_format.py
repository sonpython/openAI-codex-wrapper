"""
Human-readable rate-limit reset duration formatter.

Matches the OpenAI SDK's expected format (empirically: "7m12s", "45s", "1h05m30s").
Used for X-RateLimit-Reset-* headers and Retry-After construction.

Examples:
    format_reset(0)    -> "0s"
    format_reset(45)   -> "45s"
    format_reset(60)   -> "1m00s"
    format_reset(432)  -> "7m12s"
    format_reset(3600) -> "1h00m00s"
    format_reset(3930) -> "1h05m30s"
"""

from __future__ import annotations

import math


def format_reset(seconds_remaining: float | int) -> str:
    """Format a duration in seconds as a human-readable reset string.

    Format rules:
      0 – 59s   -> "{n}s"           e.g. "45s"
      60 – 3599s -> "{m}m{s:02d}s"  e.g. "7m12s"
      3600+      -> "{h}h{m:02d}m{s:02d}s"  e.g. "1h05m30s"

    Args:
        seconds_remaining: Non-negative seconds until reset. Floats are
                           ceiling-rounded so partial seconds count as full.

    Returns:
        Formatted string matching OpenAI reset header conventions.
    """
    secs = max(0, math.ceil(float(seconds_remaining)))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m{s:02d}s"
    h, remainder = divmod(secs, 3600)
    m, s = divmod(remainder, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def format_reset_ms(milliseconds_remaining: float | int) -> str:
    """Format a duration given in milliseconds as a reset string.

    Convenience wrapper that converts ms -> seconds then calls format_reset().

    Args:
        milliseconds_remaining: Non-negative milliseconds until reset.

    Returns:
        Formatted string matching OpenAI reset header conventions.
    """
    return format_reset(float(milliseconds_remaining) / 1000.0)
