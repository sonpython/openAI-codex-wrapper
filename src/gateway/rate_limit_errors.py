"""
Rate-limit 429 error builder compatible with raw ASGI middleware.

All rejection functions return an ASGI-compatible coroutine that sends
http.response.start + http.response.body directly on the ASGI send callable.
This is required because raw ASGI middleware cannot use JSONResponse(scope, ...)
without calling into Starlette internals — we construct the bytes manually.

OpenAI 429 response shape:
    {
        "error": {
            "type": "rate_limit_exceeded",
            "code": "<specific code>",
            "message": "<human message>",
            "param": null
        }
    }

Retry-After is always an integer seconds value (capped at 3600).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

# Human-readable messages keyed by rate-limit dimension code.
_MESSAGES: dict[str, str] = {
    "rpm_exceeded": (
        "Rate limit exceeded: too many requests per minute. "
        "Please slow down and retry after the indicated delay."
    ),
    "tpm_exceeded": (
        "Rate limit exceeded: too many tokens per minute. "
        "Reduce request size or retry after the indicated delay."
    ),
    "concurrent_limit_exceeded": (
        "Rate limit exceeded: too many concurrent requests. "
        "Wait for an in-flight request to complete before retrying."
    ),
    "monthly_quota_exceeded": (
        "Monthly token quota exhausted. " "Upgrade your plan or wait until the next billing period."
    ),
    "ip_pre_auth_exceeded": (
        "Too many unauthenticated requests from this IP address. "
        "Provide a valid API key or retry after 60 seconds."
    ),
}

_DEFAULT_MESSAGE = "Rate limit exceeded. Please retry after the indicated delay."

# Retry-After cap: 3600s (1 hour) — prevents absurd values leaking to clients.
_RETRY_AFTER_CAP = 3600

Send = Callable[[dict[str, Any]], Awaitable[None]]


def _build_body(code: str) -> bytes:
    payload = {
        "error": {
            "type": "rate_limit_exceeded",
            "code": code,
            "message": _MESSAGES.get(code, _DEFAULT_MESSAGE),
            "param": None,
        }
    }
    return json.dumps(payload).encode("utf-8")


async def _openai_error_response(
    send: Send,
    status: int,
    message: str,
    *,
    error_type: str = "invalid_request_error",
    code: str = "invalid_request_error",
) -> None:
    """Send an OpenAI-shaped error response for non-429 cases (e.g. 413).

    Used by C2 fix: body-too-large rejection before route sees the request.
    """
    payload = {
        "error": {
            "type": error_type,
            "code": code,
            "message": message,
            "param": None,
        }
    }
    body = json.dumps(payload).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def send_429(send: Send, code: str, retry_after_seconds: int) -> None:
    """Send a 429 response directly on the ASGI send callable.

    Constructs and transmits both http.response.start and http.response.body
    ASGI messages.  After this coroutine returns the response is fully sent.

    Args:
        send:                 ASGI send callable from middleware __call__.
        code:                 Rate-limit dimension code (e.g. "rpm_exceeded").
        retry_after_seconds:  Seconds until the client may retry (capped at 3600).
    """
    retry = min(max(1, retry_after_seconds), _RETRY_AFTER_CAP)
    body = _build_body(code)
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"retry-after", str(retry).encode()),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        }
    )
