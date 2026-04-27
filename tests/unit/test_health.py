"""
Unit tests for /healthz and /readyz endpoints.

Tests run against a bare FastAPI app (no lifespan) using httpx AsyncClient.
DB and Redis accessor functions are patched to simulate up/down states.

IMPORTANT: Patch the accessor functions (src.gateway.health.get_main_engine /
src.gateway.health.get_client), NOT the private module-level singletons.
Patching the stale `from … import _name` alias would not exercise the real
lazy-access path used in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthz_returns_200(client: AsyncClient) -> None:
    """/healthz always returns 200 with status=ok."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_returns_503_when_db_not_initialised(client: AsyncClient) -> None:
    """/readyz returns 503 when engine is None (DB not initialised)."""
    with (
        patch("src.gateway.health.get_main_engine", return_value=None),
        patch("src.gateway.health.get_client", return_value=None),
    ):
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert any("db" in e for e in body["errors"])


@pytest.mark.asyncio
async def test_readyz_returns_503_when_redis_not_initialised(client: AsyncClient) -> None:
    """/readyz returns 503 when redis client is None."""
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_engine.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    with (
        patch("src.gateway.health.get_main_engine", return_value=mock_engine),
        patch("src.gateway.health.get_client", return_value=None),
    ):
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert any("redis" in e for e in body["errors"])


@pytest.mark.asyncio
async def test_readyz_returns_200_when_all_healthy(client: AsyncClient, app: object) -> None:
    """/readyz returns 200 when DB, Redis, and Codex session are all healthy.

    H-3: default for codex_session_healthy is now False (fail-closed). Tests that
    want 200 must explicitly set app.state.codex_session_healthy = True to simulate
    the state that the lifespan poller sets after a successful probe.
    """
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_engine.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    # H-3: must explicitly set session state (simulates lifespan poller after probe)
    app.state.codex_session_healthy = True  # type: ignore[attr-defined]

    with (
        patch("src.gateway.health.get_main_engine", return_value=mock_engine),
        patch("src.gateway.health.get_client", return_value=mock_redis),
    ):
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_codex_session_unhealthy(
    client: AsyncClient, app: object
) -> None:
    """/readyz returns 503 when codex session is unhealthy (default-deny).

    H-3: codex_session_healthy defaults to False (fail-closed) when the lifespan
    has not run or the poller has not yet set the state. This test verifies the
    fail-closed behaviour by leaving app.state unset (bare test app).
    """
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_engine.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    # Explicitly set unhealthy to simulate poller-reported failure
    app.state.codex_session_healthy = False  # type: ignore[attr-defined]

    with (
        patch("src.gateway.health.get_main_engine", return_value=mock_engine),
        patch("src.gateway.health.get_client", return_value=mock_redis),
    ):
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert any("codex" in e for e in body["errors"])


@pytest.mark.asyncio
async def test_readyz_returns_503_when_db_raises(client: AsyncClient) -> None:
    """/readyz returns 503 when DB ping raises an exception."""
    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(side_effect=Exception("connection refused")),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    with (
        patch("src.gateway.health.get_main_engine", return_value=mock_engine),
        patch("src.gateway.health.get_client", return_value=mock_redis),
    ):
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert any("db" in e for e in body["errors"])
    # Error message must NOT leak internal exception details to caller
    assert not any("connection refused" in e for e in body["errors"])


@pytest.mark.asyncio
async def test_readyz_cold_start_then_init_cycle(client: AsyncClient, app: object) -> None:
    """Simulate cold-start: before init → 503; after init → 200.

    This test exercises the real lazy-accessor path. It does NOT patch the
    stale alias — it patches get_main_engine / get_client to return None
    first (cold), then real mocks (warm), confirming the accessor is called
    at request time, not at import time.
    """
    # ── Phase 1: cold start — neither engine nor client initialised ────────
    with (
        patch("src.gateway.health.get_main_engine", return_value=None),
        patch("src.gateway.health.get_client", return_value=None),
    ):
        cold_response = await client.get("/readyz")

    assert cold_response.status_code == 503
    cold_body = cold_response.json()
    assert cold_body["status"] == "unavailable"
    assert any("db" in e for e in cold_body["errors"])
    assert any("redis" in e for e in cold_body["errors"])

    # ── Phase 2: after init — all three healthy ───────────────────────────
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_engine.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    # H-3: explicitly set codex session healthy (simulates lifespan poller success)
    app.state.codex_session_healthy = True  # type: ignore[attr-defined]

    with (
        patch("src.gateway.health.get_main_engine", return_value=mock_engine),
        patch("src.gateway.health.get_client", return_value=mock_redis),
    ):
        warm_response = await client.get("/readyz")

    assert warm_response.status_code == 200
    assert warm_response.json()["status"] == "ok"
