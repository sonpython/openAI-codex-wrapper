"""
OpenAI-compatible error envelope helpers for auth failures.

Per researcher-02 §A.4 + §B.3.13, error bodies must match the OpenAI SDK's
expected shape exactly so SDK exception types (AuthenticationError, etc.) parse
cleanly. Wrong shape causes the SDK to raise a generic APIError instead.

Expected shape:
    {
        "error": {
            "message": "...",
            "type":    "invalid_request_error",
            "param":   null,
            "code":    "invalid_api_key"
        }
    }

All auth errors use the SAME envelope regardless of whether the token is
missing, malformed, unknown, or revoked. Uniform errors prevent enumeration
attacks (caller cannot distinguish "key doesn't exist" vs "key is revoked").
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


def _error_body(
    message: str,
    error_type: str,
    code: str,
    param: str | None = None,
) -> dict[str, object]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def invalid_api_key_response() -> JSONResponse:
    """401 — missing, malformed, unknown, or revoked bearer token.

    Identical body for all cases to prevent enumeration oracles.
    """
    return JSONResponse(
        status_code=401,
        content=_error_body(
            message="Incorrect API key provided.",
            error_type="invalid_request_error",
            code="invalid_api_key",
        ),
    )


def permission_denied_response(message: str = "Permission denied.") -> JSONResponse:
    """403 — authenticated but not authorised (e.g. wrong admin token)."""
    return JSONResponse(
        status_code=403,
        content=_error_body(
            message=message,
            error_type="invalid_request_error",
            code="permission_denied",
        ),
    )


def internal_error_response() -> JSONResponse:
    """500 — unexpected server-side error; detail intentionally withheld."""
    return JSONResponse(
        status_code=500,
        content=_error_body(
            message="An internal error occurred. Please try again.",
            error_type="api_error",
            code="internal_error",
        ),
    )


def service_unavailable_response() -> JSONResponse:
    """503 — DB pool exhausted or DB unreachable; client should retry."""
    return JSONResponse(
        status_code=503,
        content=_error_body(
            message="Service temporarily unavailable. Please retry after a moment.",
            error_type="api_error",
            code="service_unavailable",
        ),
        headers={"Retry-After": "5"},
    )
