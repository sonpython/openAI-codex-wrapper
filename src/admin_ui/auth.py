"""
Cookie session auth for admin UI.

Uses stdlib hmac + secrets — no itsdangerous dependency.

Cookie format:  {sid}.{hmac_hex}
Secret derived: HMAC-SHA256(ADMIN_TOKEN, b"admin-session-signing-key")
Redis key:      admin_session:{sid}
TTL:            settings.admin_session_ttl_seconds (default 28800 = 8h)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from redis.asyncio import Redis

_SIGNING_CONTEXT = b"admin-session-signing-key"
_SID_BYTES = 32
_COOKIE_SEP = "."


def _derive_secret(admin_token: str) -> bytes:
    """Derive a stable signing secret from the admin token.

    Uses HMAC-SHA256 keyed on the raw token so rotating ADMIN_TOKEN
    automatically invalidates all existing sessions.
    """
    return hmac.new(
        admin_token.encode(),
        _SIGNING_CONTEXT,
        hashlib.sha256,
    ).digest()


def sign_session(sid: str, admin_token: str) -> str:
    """Return a signed cookie value: ``{sid}.{hmac_hex}``."""
    secret = _derive_secret(admin_token)
    mac = hmac.new(secret, sid.encode(), hashlib.sha256).hexdigest()
    return f"{sid}{_COOKIE_SEP}{mac}"


def verify_session(cookie_value: str, admin_token: str) -> str | None:
    """Verify cookie signature and return sid, or None if invalid/tampered."""
    if not cookie_value or _COOKIE_SEP not in cookie_value:
        return None

    parts = cookie_value.split(_COOKIE_SEP, 1)
    if len(parts) != 2:
        return None

    sid, provided_mac = parts
    if not sid or not provided_mac:
        return None

    secret = _derive_secret(admin_token)
    expected_mac = hmac.new(secret, sid.encode(), hashlib.sha256).hexdigest()

    # Constant-time comparison to prevent timing attacks.
    if not secrets.compare_digest(expected_mac, provided_mac):
        return None

    return sid


def new_sid() -> str:
    """Generate a cryptographically random 32-byte URL-safe session ID."""
    return secrets.token_urlsafe(_SID_BYTES)


async def create_session(
    redis: Redis[Any],
    ttl_seconds: int,
) -> str:
    """Create a new session in Redis and return the sid.

    Stores a placeholder value (b"1"); only presence matters for auth.
    Caller should immediately sign and set the cookie.
    """
    sid = new_sid()
    key = _redis_key(sid)
    await redis.set(key, b"1", ex=ttl_seconds)
    return sid


async def validate_session(redis: Redis[Any], sid: str) -> bool:
    """Return True if the session exists in Redis (not expired)."""
    if not sid:
        return False
    exists = await redis.exists(_redis_key(sid))
    return bool(exists)


async def delete_session(redis: Redis[Any], sid: str) -> None:
    """Remove the session from Redis immediately (logout)."""
    if sid:
        await redis.delete(_redis_key(sid))


def _redis_key(sid: str) -> str:
    return f"admin_session:{sid}"
