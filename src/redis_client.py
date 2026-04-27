"""
Async Redis connection pool singleton.

Provides a single shared pool across all requests.  ``get_redis()`` is a
FastAPI dependency that yields the pool; caller does not need to manage
connection lifecycle (redis-py's connection pool handles it).

Usage (FastAPI dep):
    async def route(redis: Redis = Depends(get_redis)) -> ...:
        await redis.ping()

Usage (lifespan):
    init_redis(settings)
    ...
    await close_redis()
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

from src.settings import Settings

logger = structlog.get_logger(__name__)

# Type aliases: redis-py generics require Any for compatibility with mypy.
_pool: ConnectionPool[Any] | None = None
_client: Redis[Any] | None = None


def get_client() -> Redis[Any] | None:
    """Return the shared Redis client, or None if not yet initialised."""
    return _client


def init_redis(settings: Settings) -> None:
    """Create the connection pool.  Called once from the app lifespan."""
    global _pool, _client
    _pool = ConnectionPool.from_url(
        settings.redis_url,
        max_connections=50,
        decode_responses=False,
    )
    _client = Redis(connection_pool=_pool)
    logger.info("redis_pool_created", url=settings.redis_url)


async def close_redis() -> None:
    """Drain the pool.  Called from lifespan shutdown."""
    global _client, _pool
    if _client is not None:
        # redis-py 5.x uses aclose() on the async client;
        # fall back to close() if aclose() unavailable (older stubs).
        close_fn = getattr(_client, "aclose", None) or _client.close
        await close_fn()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None


async def get_redis() -> AsyncGenerator[Redis[Any], None]:
    """FastAPI dependency: yield the shared Redis client."""
    assert _client is not None, "Redis not initialised — call init_redis() first"
    yield _client
