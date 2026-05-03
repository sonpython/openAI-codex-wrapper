"""
Unit tests for /admin/tiers endpoints (admin_tiers.py).

NOTE: Omits `from __future__ import annotations` — FastAPI resolves route
parameter annotations eagerly; PEP 563 lazy strings break Pydantic/FastAPI.

Covers:
  - GET /admin/tiers: missing token -> 403
  - GET /admin/tiers: wrong token -> 403
  - GET /admin/tiers: correct token -> 200, list of tiers
  - PUT /admin/tiers/{tier}: missing token -> 403
  - PUT /admin/tiers/{tier}: invalid tier name -> 400
  - PUT /admin/tiers/{tier}: negative rpm -> 422
  - PUT /admin/tiers/{tier}: negative tpm -> 422
  - PUT /admin/tiers/{tier}: negative concurrent -> 422
  - PUT /admin/tiers/{tier}: negative monthly_quota -> 422
  - PUT /admin/tiers/{tier}: valid payload -> 200, cache invalidated
  - PUT /admin/tiers/{tier}: free tier upsert succeeds
  - invalidate_cache called after successful PUT
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")

_ADMIN_TOKEN = "test-admin-secret"
_WRONG_TOKEN = "bad-token"


def _mock_plan(
    tier: str = "free",
    rpm: int = 20,
    tpm: int = 20000,
    concurrent: int = 2,
    monthly_tokens: int = 100000,
) -> MagicMock:
    plan = MagicMock()
    plan.tier = tier
    plan.rpm = rpm
    plan.tpm = tpm
    plan.concurrent = concurrent
    plan.monthly_tokens = monthly_tokens
    plan.created_at = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001
    return plan


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    return session


def _make_app(mock_session: MagicMock):
    from fastapi import FastAPI
    from src.db.engine import get_session
    from src.gateway.routes.admin_tiers import router
    from src.settings import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(router, prefix="/admin")

    async def _override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture()
async def mock_session() -> MagicMock:
    return _make_mock_session()


@pytest_asyncio.fixture()
async def client(mock_session: MagicMock) -> AsyncGenerator[AsyncClient, None]:
    app = _make_app(mock_session)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Admin token guard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tiers_missing_token_returns_403(client: AsyncClient) -> None:
    response = await client.get("/admin/tiers")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_tiers_wrong_token_returns_403(client: AsyncClient) -> None:
    response = await client.get("/admin/tiers", headers={"X-Admin-Token": _WRONG_TOKEN})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_update_tier_missing_token_returns_403(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/tiers/free",
        json={"rpm": 10, "tpm": 1000, "concurrent": 2, "monthly_quota": 50000},
    )
    assert response.status_code == 403


# ── GET /admin/tiers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tiers_returns_200_with_plan_list(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    plans = [_mock_plan("free"), _mock_plan("pro", rpm=200, tpm=200000)]

    with patch(
        "src.gateway.routes.admin_tiers.plans_crud.list_all",
        new=AsyncMock(return_value=plans),
    ):
        response = await client.get(
            "/admin/tiers",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["tier"] == "free"
    assert body[1]["tier"] == "pro"
    assert body[1]["rpm"] == 200


@pytest.mark.asyncio
async def test_list_tiers_returns_empty_list_when_no_plans(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    with patch(
        "src.gateway.routes.admin_tiers.plans_crud.list_all",
        new=AsyncMock(return_value=[]),
    ):
        response = await client.get(
            "/admin/tiers",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200
    assert response.json() == []


# ── PUT /admin/tiers/{tier} — validation ───────────────────────────────────


@pytest.mark.asyncio
async def test_update_invalid_tier_name_returns_400(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/tiers/invalid_tier",
        json={"rpm": 10, "tpm": 1000, "concurrent": 2, "monthly_quota": 50000},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 400
    assert "invalid_tier" in response.json()["detail"]


@pytest.mark.asyncio
async def test_update_tier_negative_rpm_returns_422(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/tiers/free",
        json={"rpm": -1, "tpm": 1000, "concurrent": 2, "monthly_quota": 50000},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_tier_negative_tpm_returns_422(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/tiers/free",
        json={"rpm": 10, "tpm": -500, "concurrent": 2, "monthly_quota": 50000},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_tier_negative_concurrent_returns_422(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/tiers/free",
        json={"rpm": 10, "tpm": 1000, "concurrent": -1, "monthly_quota": 50000},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_tier_negative_monthly_quota_returns_422(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/tiers/free",
        json={"rpm": 10, "tpm": 1000, "concurrent": 2, "monthly_quota": -1},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 422


# ── PUT /admin/tiers/{tier} — success ─────────────────────────────────────


@pytest.mark.asyncio
async def test_update_tier_free_returns_200(client: AsyncClient, mock_session: MagicMock) -> None:
    updated = _mock_plan("free", rpm=30, tpm=30000, concurrent=3, monthly_tokens=200000)

    with (
        patch(
            "src.gateway.routes.admin_tiers.plans_crud.update",
            new=AsyncMock(return_value=updated),
        ),
        patch("src.gateway.routes.admin_tiers.invalidate_cache") as mock_invalidate,
    ):
        response = await client.put(
            "/admin/tiers/free",
            json={"rpm": 30, "tpm": 30000, "concurrent": 3, "monthly_quota": 200000},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "free"
    assert body["rpm"] == 30
    assert body["tpm"] == 30000
    assert body["concurrent"] == 3
    assert body["monthly_tokens"] == 200000
    mock_invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_update_tier_pro_returns_200(client: AsyncClient, mock_session: MagicMock) -> None:
    updated = _mock_plan("pro", rpm=500, tpm=500000, concurrent=20, monthly_tokens=5000000)

    with (
        patch(
            "src.gateway.routes.admin_tiers.plans_crud.update",
            new=AsyncMock(return_value=updated),
        ),
        patch("src.gateway.routes.admin_tiers.invalidate_cache"),
    ):
        response = await client.put(
            "/admin/tiers/pro",
            json={"rpm": 500, "tpm": 500000, "concurrent": 20, "monthly_quota": 5000000},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200
    assert response.json()["tier"] == "pro"
    assert response.json()["rpm"] == 500


@pytest.mark.asyncio
async def test_update_tier_invalidate_cache_called_on_success(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    """Verify invalidate_cache() is called in the same request as DB update."""
    updated = _mock_plan("ent")

    with (
        patch(
            "src.gateway.routes.admin_tiers.plans_crud.update",
            new=AsyncMock(return_value=updated),
        ),
        patch("src.gateway.routes.admin_tiers.invalidate_cache") as mock_invalidate,
    ):
        response = await client.put(
            "/admin/tiers/ent",
            json={"rpm": 2000, "tpm": 2000000, "concurrent": 50, "monthly_quota": 20000000},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200
    # Critical: invalidate_cache MUST be called to reflect changes immediately.
    mock_invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_update_tier_zero_values_allowed(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    """Values of 0 are valid (means 'unlimited' in gateway middleware)."""
    updated = _mock_plan("free", rpm=0, tpm=0, concurrent=0, monthly_tokens=0)

    with (
        patch(
            "src.gateway.routes.admin_tiers.plans_crud.update",
            new=AsyncMock(return_value=updated),
        ),
        patch("src.gateway.routes.admin_tiers.invalidate_cache"),
    ):
        response = await client.put(
            "/admin/tiers/free",
            json={"rpm": 0, "tpm": 0, "concurrent": 0, "monthly_quota": 0},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_update_tier_enterprise_name_accepted(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    """'enterprise' is a valid alias alongside 'ent'."""
    updated = _mock_plan(
        "enterprise", rpm=2000, tpm=2000000, concurrent=50, monthly_tokens=20000000
    )

    with (
        patch(
            "src.gateway.routes.admin_tiers.plans_crud.update",
            new=AsyncMock(return_value=updated),
        ),
        patch("src.gateway.routes.admin_tiers.invalidate_cache"),
    ):
        response = await client.put(
            "/admin/tiers/enterprise",
            json={"rpm": 2000, "tpm": 2000000, "concurrent": 50, "monthly_quota": 20000000},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 200
