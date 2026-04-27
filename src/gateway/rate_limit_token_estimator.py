"""
Token cost estimator for rate-limit pre-charging.

Peeks at the ASGI request body exactly once, caches raw bytes and the
estimated token count on scope["state"] so downstream middleware and routes
read the same cached copy — no double-read of the HTTP body.

Endpoints:
  /v1/chat/completions  — tiktoken on prompt messages + max_tokens cap
  /v1/responses         — tiktoken on input + instructions + max_output_tokens
  /v1/codex/jobs*       — return 0 (jobs are runtime-bound, not token-bound)
  everything else       — return 0

Body-replay shim:
  After peeking, a replay_receive coroutine is installed on scope["state"]
  so the route handler's Depends(Request) body read gets the same bytes.
  Pattern: store raw bytes; replace receive with a one-shot replay then pass
  through to the real receive.

H4 fix: only peek body for POST/PUT/PATCH methods — GET/HEAD/DELETE/OPTIONS
do not have a meaningful body and calling receive() on them can stall.

C2 fix: hard cap on buffered bytes (PEEK_MAX_BYTES). If exceeded, abort
immediately with a 413 OpenAI-shaped error before running the estimator.
The route never sees an oversized request. Partial body is NOT replayed —
caller gets 413 response directly.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Try to import tiktoken; fall back gracefully if unavailable.
try:
    import tiktoken as _tiktoken

    _enc = _tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _enc = None  # type: ignore[assignment]

_DEFAULT_MAX_TOKENS = 1024

# C2: Hard cap on body buffering.  Requests exceeding this get a 413 before
# estimation runs.  256 KB is generous for a typical chat/responses prompt.
PEEK_MAX_BYTES = 256_000

# H4: Only peek body for write-method requests.
_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})

Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]


def _count_tokens(text: str) -> int:
    if _enc is not None:
        try:
            return len(_enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


def _estimate_chat(body_bytes: bytes) -> int:
    """Estimate tokens for /v1/chat/completions."""
    try:
        data = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return _DEFAULT_MAX_TOKENS

    messages = data.get("messages", [])
    prompt_text = " ".join((m.get("content") or "") for m in messages if isinstance(m, dict))
    prompt_tokens = _count_tokens(prompt_text)
    max_tokens = int(data.get("max_tokens") or _DEFAULT_MAX_TOKENS)
    return prompt_tokens + max_tokens


def _estimate_responses(body_bytes: bytes) -> int:
    """Estimate tokens for /v1/responses."""
    try:
        data = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return _DEFAULT_MAX_TOKENS

    input_val = data.get("input", "")
    if isinstance(input_val, list):
        input_text = " ".join(
            (item.get("content") or "") if isinstance(item, dict) else str(item)
            for item in input_val
        )
    else:
        input_text = str(input_val or "")

    instructions = str(data.get("instructions") or "")
    prompt_tokens = _count_tokens(input_text + " " + instructions)
    max_tokens = int(data.get("max_output_tokens") or _DEFAULT_MAX_TOKENS)
    return prompt_tokens + max_tokens


async def peek_and_estimate(
    scope: MutableMapping[str, Any],
    receive: Receive,
) -> tuple[int, Receive] | tuple[None, None]:
    """Read the request body once, estimate token cost, cache on scope.

    Returns:
        (estimated_tokens, replay_receive) on success — replay_receive is a
        drop-in replacement for the original receive that replays the cached
        body bytes to the next consumer (FastAPI route).

        (None, None) on C2 cap-exceeded: caller MUST send a 413 response
        immediately and not pass the request to the route. Sending partial
        body downstream would result in malformed JSON errors, not the clean
        rejection the client needs.

    Side-effects:
        scope["state"]["_body_bytes"] = raw body bytes (for debug/audit)
        scope["state"]["tpm_estimated_cost"] = int estimated tokens

    Zero cost paths (no body read):
        /v1/codex/jobs*, /v1/models, unknown paths → return (0, receive)
        GET/HEAD/DELETE/OPTIONS methods → return (0, receive) [H4 fix]
    """
    path: str = scope.get("path", "")
    method: str = scope.get("method", "GET").upper()

    # H4: Skip body peek for non-write methods — GET/HEAD/DELETE/OPTIONS carry no body.
    if method not in _BODY_METHODS:
        scope.setdefault("state", {})["tpm_estimated_cost"] = 0
        return 0, receive

    if path.startswith(("/v1/codex/jobs", "/v1/models")):
        scope.setdefault("state", {})["tpm_estimated_cost"] = 0
        return 0, receive

    if path not in ("/v1/chat/completions", "/v1/responses"):
        scope.setdefault("state", {})["tpm_estimated_cost"] = 0
        return 0, receive

    # Check cache — avoid double-read if another middleware already peeked.
    state: dict[str, Any] = scope.setdefault("state", {})
    if "_body_bytes" in state:
        cached_bytes: bytes = state["_body_bytes"]
        return int(state.get("tpm_estimated_cost", 0)), _make_replay(cached_bytes, receive)

    # C2: Check Content-Length header first — fast path before reading any bytes.
    headers = scope.get("headers", [])
    for hk, hv in headers:
        if hk.lower() == b"content-length":
            try:
                cl = int(hv)
                if cl > PEEK_MAX_BYTES:
                    logger.warning(
                        "token_estimator.body_too_large_content_length",
                        content_length=cl,
                        cap=PEEK_MAX_BYTES,
                        path=path,
                    )
                    return None, None
            except ValueError:
                pass
            break

    # Drain the body from ASGI receive with byte cap.
    body_chunks: list[bytes] = []
    total_bytes = 0
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.request":
            chunk = message.get("body", b"")
            total_bytes += len(chunk)
            # C2: enforce hard cap — return sentinel immediately.
            if total_bytes > PEEK_MAX_BYTES:
                logger.warning(
                    "token_estimator.body_too_large",
                    total_bytes=total_bytes,
                    cap=PEEK_MAX_BYTES,
                    path=path,
                )
                return None, None
            body_chunks.append(chunk)
            more = message.get("more_body", False)
        else:
            # Non-body message (disconnect etc.) — stop draining.
            break

    body_bytes = b"".join(body_chunks)
    state["_body_bytes"] = body_bytes

    try:
        if path == "/v1/chat/completions":
            cost = _estimate_chat(body_bytes)
        else:
            cost = _estimate_responses(body_bytes)
    except Exception:  # noqa: BLE001
        logger.warning("token_estimator.estimation_failed", path=path, exc_info=True)
        cost = _DEFAULT_MAX_TOKENS

    state["tpm_estimated_cost"] = cost
    return cost, _make_replay(body_bytes, receive)


def _make_replay(body_bytes: bytes, original_receive: Receive) -> Receive:
    """Return a one-shot replay receive that re-emits the cached body bytes.

    After the cached body is replayed, subsequent calls delegate to the
    original receive (for disconnect / other message types).
    """
    replayed = False

    async def replay_receive() -> MutableMapping[str, Any]:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        return await original_receive()

    return replay_receive
