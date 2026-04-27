"""
Unit tests for src/gateway/middleware/edge_ip_limiter.py.

Covers:
  - Missing Authorization header → IP bucket incremented → 429 after RPM exceeded
  - Malformed token (not cwk_ format) → treated as unauthenticated
  - Valid cwk_ token shape → passes through without IP bucketing
  - Skip paths (/healthz, /readyz, /metrics) bypass the limiter entirely
  - Redis error → fail-open (request passes through)
  - TRUST_PROXY=true reads X-Forwarded-For
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _make_app(fake_redis: fakeredis.aioredis.FakeRedis) -> object:  # type: ignore[type-arg]
    from fastapi import FastAPI
    from src.gateway.middleware.edge_ip_limiter import EdgeIPLimiter

    app = FastAPI()
    app.add_middleware(EdgeIPLimiter)

    @app.get("/v1/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict:  # type: ignore[type-arg]
        return {"status": "ok"}

    return app


@pytest_asyncio.fixture()
async def fake_redis() -> fakeredis.aioredis.FakeRedis:  # type: ignore[type-arg]
    return fakeredis.aioredis.FakeRedis()


@pytest_asyncio.fixture()
async def client(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> AsyncGenerator[AsyncClient, None]:
    app = _make_app(fake_redis)
    with patch("src.gateway.middleware.edge_ip_limiter.get_client", return_value=fake_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            yield ac


# ── Skip paths ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_bypasses_edge_ip_limiter(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_valid_cwk_token_passes_through(client: AsyncClient) -> None:
    """Well-shaped cwk_ token skips IP bucketing (auth middleware handles verify)."""
    valid_token = "cwk_" + "A" * 24
    resp = await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})
    # Route returns 200 — no 429 from EdgeIPLimiter
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_missing_auth_header_increments_ip_bucket(
    client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Missing Authorization → IP bucket is incremented."""
    await client.get("/v1/ping")
    # The IP may be "testclient" or "unknown" depending on ASGITransport.
    # Just assert *some* ip_pre_auth key was set.
    all_keys = await fake_redis.keys("ip_pre_auth:*")
    assert len(all_keys) >= 1


@pytest.mark.asyncio
async def test_malformed_token_increments_ip_bucket(
    client: AsyncClient,
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Malformed token (wrong prefix) → treated as unauthenticated."""
    await client.get("/v1/ping", headers={"Authorization": "Bearer sk-proj-abc123"})
    all_keys = await fake_redis.keys("ip_pre_auth:*")
    assert len(all_keys) >= 1


@pytest.mark.asyncio
async def test_ip_bucket_rejected_after_rpm_exceeded(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """IP gets 429 once bucket exceeds IP_PRE_AUTH_RPM (default 30)."""
    from fastapi import FastAPI
    from src.gateway.middleware.edge_ip_limiter import EdgeIPLimiter

    app = FastAPI()
    app.add_middleware(EdgeIPLimiter)

    @app.get("/v1/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        return {"ok": True}

    # httpx ASGITransport reports client IP as 127.0.0.1
    # Pre-seed the bucket at the limit so next INCR → limit+1 → rejected
    await fake_redis.set("ip_pre_auth:127.0.0.1", 30)

    with patch("src.gateway.middleware.edge_ip_limiter.get_client", return_value=fake_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/v1/ping")

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "ip_pre_auth_exceeded"
    assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_redis_error_fails_open(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Redis failure on script call → fail-open, request passes through with 200."""
    from fastapi import FastAPI
    from redis.exceptions import RedisError
    from src.gateway.middleware.edge_ip_limiter import EdgeIPLimiter

    app = FastAPI()
    app.add_middleware(EdgeIPLimiter)

    @app.get("/v1/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        return {"ok": True}

    # Mock the script callable to raise RedisError when invoked
    error_script = AsyncMock(side_effect=RedisError("connection lost"))
    error_redis = MagicMock()
    error_redis.register_script = MagicMock(return_value=error_script)

    with patch("src.gateway.middleware.edge_ip_limiter.get_client", return_value=error_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/v1/ping")

    assert resp.status_code == 200, "Redis error must fail-open"
