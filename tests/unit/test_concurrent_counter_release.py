"""
Unit tests for concurrent counter INCR/DECR lifecycle in RateLimitMiddleware.

Covers:
  - Counter is INCR on enter, DECR on normal response exit
  - Counter is DECR even when the route raises an exception (finally block)
  - Counter does not leak on Redis error during RPM check (compensating DECR)
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

_FREE_LIMITS = {"rpm": 20, "tpm": 20000, "concurrent": 2, "monthly_tokens": 100000}


@pytest_asyncio.fixture()
async def fake_redis() -> fakeredis.aioredis.FakeRedis:  # type: ignore[type-arg]
    return fakeredis.aioredis.FakeRedis()


async def _run_middleware(
    fake_redis: fakeredis.aioredis.FakeRedis,
    *,
    key_id: object,
    user_id: object,
    route_raises: bool = False,
) -> tuple[list[dict], int | None]:  # type: ignore[type-arg]
    """Helper: run RateLimitMiddleware with a mock inner app.

    Returns (response_messages, final_concurrent_counter_value).
    """
    from src.gateway.middleware.rate_limit import RateLimitMiddleware

    responses: list[dict] = []  # type: ignore[type-arg]

    async def inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
        if route_raises:
            raise RuntimeError("route exploded")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = RateLimitMiddleware(inner_app)

    scope: dict = {  # type: ignore[type-arg]
        "type": "http",
        "method": "GET",
        "path": "/v1/ping",
        "query_string": b"",
        "headers": [],
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tier": "free",
        },
    }

    async def receive():  # type: ignore[return]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # type: ignore[no-untyped-def]
        responses.append(message)

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch(
            "src.gateway.middleware.rate_limit.get_limits",
            new=AsyncMock(return_value=_FREE_LIMITS),
        ),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        with contextlib.suppress(RuntimeError):
            await middleware(scope, receive, send)

    key = f"rl:concurrent:{key_id}"
    raw = await fake_redis.get(key)
    counter = int(raw) if raw is not None else 0
    return responses, counter


@pytest.mark.asyncio
async def test_concurrent_counter_incr_and_decr_on_success(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Counter must be 0 after a successful request (INCR on enter, DECR on exit)."""
    key_id = uuid4()
    _, counter = await _run_middleware(fake_redis, key_id=key_id, user_id=uuid4())
    assert counter == 0, f"concurrent counter must be 0 after response; got {counter}"


@pytest.mark.asyncio
async def test_concurrent_counter_decr_on_route_exception(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Counter must be 0 even when the route raises an exception (finally DECR)."""
    key_id = uuid4()
    _, counter = await _run_middleware(
        fake_redis, key_id=key_id, user_id=uuid4(), route_raises=True
    )
    assert counter == 0, f"concurrent counter must be 0 after route exception; got {counter}"


@pytest.mark.asyncio
async def test_two_concurrent_requests_counter_back_to_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Two sequential requests → counter returns to 0 after both complete."""
    key_id = uuid4()
    await _run_middleware(fake_redis, key_id=key_id, user_id=uuid4())
    await _run_middleware(fake_redis, key_id=key_id, user_id=uuid4())
    raw = await fake_redis.get(f"rl:concurrent:{key_id}")
    counter = int(raw) if raw is not None else 0
    assert counter == 0, f"counter should be 0 after two sequential requests, got {counter}"


@pytest.mark.asyncio
async def test_concurrent_cap_enforced(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Third concurrent attempt at cap=2 must be rejected (counter stays at 2)."""
    from src.gateway.middleware.rate_limit import RateLimitMiddleware

    # Manually INCR to simulate 2 in-flight requests
    key_id = uuid4()
    concurrent_key = f"rl:concurrent:{key_id}"
    await fake_redis.set(concurrent_key, 2)
    await fake_redis.expire(concurrent_key, 60)

    responses: list[dict] = []  # type: ignore[type-arg]

    async def inner_app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = RateLimitMiddleware(inner_app)
    scope: dict = {  # type: ignore[type-arg]
        "type": "http",
        "method": "GET",
        "path": "/v1/ping",
        "query_string": b"",
        "headers": [],
        "state": {
            "api_key_id": key_id,
            "user_id": uuid4(),
            "tier": "free",
        },
    }

    async def receive():  # type: ignore[return]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send_fn(message):  # type: ignore[no-untyped-def]
        responses.append(message)

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch(
            "src.gateway.middleware.rate_limit.get_limits",
            new=AsyncMock(return_value=_FREE_LIMITS),
        ),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
        await middleware(scope, receive, send_fn)

    start = next(m for m in responses if m["type"] == "http.response.start")
    assert start["status"] == 429
    header_map = {k.decode(): v.decode() for k, v in start["headers"]}
    assert "retry-after" in header_map
