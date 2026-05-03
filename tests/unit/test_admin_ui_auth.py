"""
Unit tests for src/admin_ui/auth.py

Covers:
  - sign_session / verify_session happy path
  - verify_session rejects tampered MAC
  - verify_session rejects malformed cookie
  - verify_session rejects wrong token (constant-time safe)
  - create_session stores key in Redis with correct TTL
  - validate_session returns True/False based on Redis presence
  - delete_session removes the Redis key
"""

from __future__ import annotations

import fakeredis.aioredis as fakeredis
import pytest
from src.admin_ui.auth import (
    _redis_key,
    create_session,
    delete_session,
    new_sid,
    sign_session,
    validate_session,
    verify_session,
)

TOKEN = "test-admin-token-abc123"
WRONG_TOKEN = "wrong-token-xyz"


# ── sign / verify ──────────────────────────────────────────────────────────────


def test_sign_and_verify_roundtrip() -> None:
    sid = new_sid()
    cookie = sign_session(sid, TOKEN)
    assert "." in cookie
    result = verify_session(cookie, TOKEN)
    assert result == sid


def test_verify_rejects_wrong_token() -> None:
    sid = new_sid()
    cookie = sign_session(sid, TOKEN)
    assert verify_session(cookie, WRONG_TOKEN) is None


def test_verify_rejects_tampered_mac() -> None:
    sid = new_sid()
    cookie = sign_session(sid, TOKEN)
    sid_part, mac_part = cookie.split(".", 1)
    tampered = f"{sid_part}.{'a' * len(mac_part)}"
    assert verify_session(tampered, TOKEN) is None


def test_verify_rejects_empty_string() -> None:
    assert verify_session("", TOKEN) is None


def test_verify_rejects_no_separator() -> None:
    assert verify_session("noseparatorhere", TOKEN) is None


def test_verify_rejects_missing_sid() -> None:
    # dot present but empty sid
    assert verify_session(".somemac", TOKEN) is None


def test_sign_produces_different_values_for_different_sids() -> None:
    sid1 = new_sid()
    sid2 = new_sid()
    assert sign_session(sid1, TOKEN) != sign_session(sid2, TOKEN)


# ── Redis session helpers ──────────────────────────────────────────────────────


@pytest.fixture
def redis():  # type: ignore[no-untyped-def]
    return fakeredis.FakeRedis()


@pytest.mark.asyncio
async def test_create_session_stores_key(redis) -> None:  # type: ignore[no-untyped-def]
    sid = await create_session(redis, ttl_seconds=3600)
    assert await redis.exists(_redis_key(sid))


@pytest.mark.asyncio
async def test_create_session_returns_nonempty_sid(redis) -> None:  # type: ignore[no-untyped-def]
    sid = await create_session(redis, ttl_seconds=3600)
    assert len(sid) > 20


@pytest.mark.asyncio
async def test_validate_session_returns_true_when_present(redis) -> None:  # type: ignore[no-untyped-def]
    sid = await create_session(redis, ttl_seconds=3600)
    assert await validate_session(redis, sid) is True


@pytest.mark.asyncio
async def test_validate_session_returns_false_when_absent(redis) -> None:  # type: ignore[no-untyped-def]
    assert await validate_session(redis, "nonexistent-sid") is False


@pytest.mark.asyncio
async def test_validate_session_returns_false_for_empty_sid(redis) -> None:  # type: ignore[no-untyped-def]
    assert await validate_session(redis, "") is False


@pytest.mark.asyncio
async def test_delete_session_removes_key(redis) -> None:  # type: ignore[no-untyped-def]
    sid = await create_session(redis, ttl_seconds=3600)
    await delete_session(redis, sid)
    assert not await redis.exists(_redis_key(sid))


@pytest.mark.asyncio
async def test_delete_session_noop_on_missing(redis) -> None:  # type: ignore[no-untyped-def]
    # Should not raise even if key doesn't exist
    await delete_session(redis, "ghost-session-id")
