"""
Shared pytest fixtures for the codex-wrapper test suite.

Provides:
- ``settings``: a Settings instance with safe test values (no real DB/Redis needed
  for unit tests — integration tests requiring live services live in tests/integration/).
- ``app``: a FastAPI test application with lifespan disabled so unit tests
  don't need a running Postgres/Redis.
- ``client``: an httpx.AsyncClient bound to the test app.

The DATABASE_URL and REDIS_URL env vars are always set here so pydantic-settings
doesn't raise on import during unit-test runs that don't have a real DB.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Set required env vars before any src imports so Settings() doesn't raise.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(scope="session")
def settings():  # type: ignore[return]
    """Return a Settings instance configured for testing."""
    # Import here (after env vars set above) to avoid ValidationError.
    from src.settings import Settings  # noqa: PLC0415

    return Settings(
        wrapper_env="test",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/0",
        log_level="WARNING",
    )


@pytest.fixture()
def app():  # type: ignore[return]
    """Return a bare FastAPI app without lifespan for unit tests.

    DB and Redis pools are NOT initialised; tests that need them must mock
    the relevant module-level singletons directly.
    """
    from fastapi import FastAPI  # noqa: PLC0415
    from src.gateway.health import router as health_router  # noqa: PLC0415
    from src.observability.metrics import make_metrics_app  # noqa: PLC0415

    bare_app = FastAPI()
    bare_app.include_router(health_router)
    bare_app.mount("/metrics", make_metrics_app())
    return bare_app


@pytest_asyncio.fixture()
async def client(app) -> AsyncGenerator[AsyncClient, None]:  # type: ignore[type-arg]
    """Async HTTP client wired to the test FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
