"""
Bearer token extraction from HTTP Authorization header.

Extracts and validates the structural shape of the token before any DB lookup:
  1. Header must be present.
  2. Scheme must be "Bearer" (case-insensitive).
  3. Token must start with "cwk_" — rejects foreign tokens early, avoiding a
     needless DB query + argon2 hash attempt.

Returns the raw plaintext token string, or None on any structural failure.
Callers map None → 401 without distinguishing the failure sub-reason (prevents
enumeration: missing vs wrong scheme vs wrong prefix all look the same to client).
"""

from __future__ import annotations

from starlette.datastructures import Headers

from src.auth.hashing import KEY_PREFIX


def extract_bearer(headers: Headers) -> str | None:
    """Extract and structurally validate the Bearer token from request headers.

    Args:
        headers: Starlette Headers object from the incoming request.

    Returns:
        The plaintext token string if structurally valid, else None.
    """
    auth_header = headers.get("authorization")
    if not auth_header:
        return None

    # Split on any whitespace (space, tab, multiple spaces) per RFC 7235 BWS grammar.
    # maxsplit=1 means: ["Bearer", "<token>"] even with leading/trailing whitespace.
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2:
        return None

    scheme, token = parts
    if scheme.lower() != "bearer":
        return None

    token = token.strip()

    # Fast structural rejection — avoids argon2 hash attempt for non-platform tokens.
    if not token.startswith(KEY_PREFIX):
        return None

    return token
