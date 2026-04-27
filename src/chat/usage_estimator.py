"""
Token usage estimator using tiktoken (cl100k_base encoding).

Codex CLI does not expose its internal token counts to the wrapper; we
estimate using tiktoken on the user-visible text. This is best-effort:
Codex's system prompt + tool scaffolding are counted internally by the
upstream model but are invisible to us.

Fallback: if tiktoken is not installed or fails to load, we use the
``len(text) // 4`` heuristic (rough average for English prose).

Usage objects include ``_estimated: true`` as an extra field so
downstream monitoring can distinguish estimated from exact counts.
"""

from __future__ import annotations

import structlog

from src.gateway.schemas.chat_response import Usage

logger = structlog.get_logger(__name__)

# Pre-warm encoder at module import for faster first-request latency.
# Falls back to None if tiktoken is unavailable.
try:
    import tiktoken as _tiktoken

    _enc = _tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001  # broad: covers import + encoding errors
    _enc = None  # type: ignore[assignment]
    logger.warning("usage_estimator.tiktoken_unavailable", fallback="len//4")


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken or fall back to character heuristic."""
    if _enc is not None:
        try:
            return len(_enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


def estimate(prompt_text: str, completion_text: str) -> Usage:
    """Return a best-effort Usage object for the given prompt + completion.

    Args:
        prompt_text:      The full assembled prompt sent to Codex.
        completion_text:  The aggregated assistant response text.

    Returns:
        Usage with prompt_tokens, completion_tokens, total_tokens populated.
        Extra field ``_estimated=True`` signals best-effort accounting.
    """
    p = _count_tokens(prompt_text)
    c = _count_tokens(completion_text)
    return Usage(
        prompt_tokens=p,
        completion_tokens=c,
        total_tokens=p + c,
        _estimated=True,  # type: ignore[call-arg]  # extra field via extra=allow
    )


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate ``text`` to at most ``max_tokens`` tokens.

    Uses tiktoken token boundaries when available (safe for UTF-8 multibyte);
    falls back to character-based approximation.

    Args:
        text:       Text to truncate.
        max_tokens: Maximum token count.

    Returns:
        Truncated string (may equal input if already within limit).
    """
    if _enc is not None:
        try:
            tokens = _enc.encode(text)
            if len(tokens) <= max_tokens:
                return text
            return _enc.decode(tokens[:max_tokens])
        except Exception:  # noqa: BLE001
            pass
    # Character-heuristic fallback
    char_limit = max_tokens * 4
    return text[:char_limit]
