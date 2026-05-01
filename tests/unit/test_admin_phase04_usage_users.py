"""
Unit tests for Phase 4 — Per-User Usage + Live Metrics + Polish.

Covers:
  - parse_range: valid inputs, invalid input → ValueError
  - prom_client cache: fetch_kpis_cached returns cached value within TTL
  - prom_client cache: stale flag set when fetch fails and cache exists
  - prom_client cache: zeroed snapshot + stale when no cache and fetch fails
  - UserAggregate dataclass: field types
  - admin_users router: GET /users 403 without token
  - admin_users router: GET /users 200 with mocked aggregates
  - admin_users router: GET /users/{id}/keys 403 without token
  - admin_usage router: GET /usage/summary valid range 200
  - admin_usage router: GET /usage/summary invalid range 400
  - admin_usage router: GET /usage/summary invalid user_id 400
  - admin_usage router: GET /usage/by-key/{id} valid range 200
  - admin_usage router: GET /usage/by-key/{id} invalid range 400
  - admin_usage router: no auth → 403
  - DailyUsage schema: field types
  - users_page_routes: GET /users renders 200
  - users_page_routes: GET /users/{id} 404 for missing user
  - users_page_routes: GET /users/{id}/_chart_data invalid range 400
  - users_page_routes: GET /users/{id}/_chart_data valid range 200 JSON
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Set env vars before any src imports so pydantic-settings picks them up.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

from src.gateway.routes.admin_usage import parse_range, _VALID_RANGES

_TOKEN = "test-admin-secret"
_ADMIN_HEADERS = {"X-Admin-Token": _TOKEN}


# ── parse_range ───────────────────────────────────────────────────────────────


def test_parse_range_24h() -> None:
    assert parse_range("24h").total_seconds() == 24 * 3600


def test_parse_range_7d() -> None:
    assert parse_range("7d").total_seconds() == 7 * 24 * 3600


def test_parse_range_30d() -> None:
    assert parse_range("30d").total_seconds() == 30 * 24 * 3600


def test_parse_range_invalid_raises() -> None:
    with pytest.raises(ValueError, match="range must be one of"):
        parse_range("1h")


def test_parse_range_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_range("")


def test_parse_range_all_valid_values() -> None:
    for r in _VALID_RANGES:
        assert parse_range(r).total_seconds() > 0


# ── prom_client cache ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kpi_cache_returns_cached_within_ttl(monkeypatch: Any) -> None:
    """Second call within 5s TTL returns cached value without re-fetching."""
    import src.admin_ui.prom_client as prom

    prom._kpi_cache["snapshot"] = None
    prom._kpi_cache["fetched_at"] = 0.0
    prom._kpi_cache["stale"] = False

    call_count = 0

    async def _mock_fetch() -> prom.KPISnapshot:
        nonlocal call_count
        call_count += 1
        return prom.KPISnapshot(req_rate_1m=1.23)

    monkeypatch.setattr(prom, "fetch_kpis", _mock_fetch)

    snap1, stale1 = await prom.fetch_kpis_cached()
    snap2, stale2 = await prom.fetch_kpis_cached()

    assert call_count == 1
    assert snap1.req_rate_1m == 1.23
    assert snap2.req_rate_1m == 1.23
    assert stale1 is False
    assert stale2 is False


@pytest.mark.asyncio
async def test_kpi_cache_refreshes_after_ttl(monkeypatch: Any) -> None:
    import src.admin_ui.prom_client as prom

    call_count = 0

    async def _mock_fetch() -> prom.KPISnapshot:
        nonlocal call_count
        call_count += 1
        return prom.KPISnapshot(req_rate_1m=float(call_count))

    monkeypatch.setattr(prom, "fetch_kpis", _mock_fetch)

    # Prime with expired timestamp
    prom._kpi_cache["snapshot"] = prom.KPISnapshot(req_rate_1m=0.0)
    prom._kpi_cache["fetched_at"] = time.monotonic() - 10.0
    prom._kpi_cache["stale"] = False

    snap, stale = await prom.fetch_kpis_cached()

    assert call_count == 1
    assert snap.req_rate_1m == 1.0
    assert stale is False


@pytest.mark.asyncio
async def test_kpi_cache_returns_stale_on_fetch_failure(monkeypatch: Any) -> None:
    import src.admin_ui.prom_client as prom

    async def _failing() -> prom.KPISnapshot:
        raise RuntimeError("Prometheus unreachable")

    monkeypatch.setattr(prom, "fetch_kpis", _failing)

    old_snap = prom.KPISnapshot(req_rate_1m=9.9)
    prom._kpi_cache["snapshot"] = old_snap
    prom._kpi_cache["fetched_at"] = time.monotonic() - 10.0
    prom._kpi_cache["stale"] = False

    snap, stale = await prom.fetch_kpis_cached()

    assert stale is True
    assert snap.req_rate_1m == 9.9


@pytest.mark.asyncio
async def test_kpi_cache_zeros_and_stale_when_no_cache_and_fetch_fails(monkeypatch: Any) -> None:
    import src.admin_ui.prom_client as prom

    async def _failing() -> prom.KPISnapshot:
        raise RuntimeError("down")

    monkeypatch.setattr(prom, "fetch_kpis", _failing)

    prom._kpi_cache["snapshot"] = None
    prom._kpi_cache["fetched_at"] = 0.0
    prom._kpi_cache["stale"] = False

    snap, stale = await prom.fetch_kpis_cached()

    assert stale is True
    assert snap.req_rate_1m == 0.0


# ── UserAggregate dataclass ───────────────────────────────────────────────────


def test_user_aggregate_fields() -> None:
    from src.db.crud.users import UserAggregate

    uid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    agg = UserAggregate(
        id=uid,
        email="test@example.com",
        created_at=now,
        key_count=3,
        current_month_requests=100,
        current_month_tokens=5000,
    )
    assert agg.id == uid
    assert agg.email == "test@example.com"
    assert agg.key_count == 3
    assert agg.current_month_requests == 100
    assert agg.current_month_tokens == 5000


# ── DailyUsage schema ─────────────────────────────────────────────────────────


def test_daily_usage_schema() -> None:
    from src.gateway.routes.admin_usage import DailyUsage

    row = DailyUsage(day="2026-04-25", requests=10, tokens=500)
    assert row.day == "2026-04-25"
    assert row.requests == 10
    assert row.tokens == 500


# ── admin_users router ────────────────────────────────────────────────────────


def _make_admin_users_app() -> FastAPI:
    from src.settings import get_settings
    get_settings.cache_clear()
    from src.gateway.routes.admin_users import router
    from src.db.engine import get_session

    app = FastAPI()
    app.include_router(router, prefix="/admin")

    async def _mock_db() -> AsyncGenerator[MagicMock, None]:
        yield MagicMock()

    app.dependency_overrides[get_session] = _mock_db
    return app


@pytest.mark.asyncio
async def test_list_users_no_auth_returns_403() -> None:
    app = _make_admin_users_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/admin/users")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_users_returns_200_with_mocked_aggregates() -> None:
    from src.db.crud.users import UserAggregate
    from src.settings import get_settings
    get_settings.cache_clear()

    uid = uuid.uuid4()
    mock_agg = UserAggregate(
        id=uid,
        email="alice@example.com",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        key_count=2,
        current_month_requests=50,
        current_month_tokens=1000,
    )

    with patch("src.gateway.routes.admin_users.list_with_aggregates", new=AsyncMock(return_value=([mock_agg], 1))):
        app = _make_admin_users_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/users", headers=_ADMIN_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "alice@example.com"
    assert body["items"][0]["key_count"] == 2


@pytest.mark.asyncio
async def test_list_user_keys_no_auth_returns_403() -> None:
    app = _make_admin_users_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/admin/users/{uuid.uuid4()}/keys")
    assert resp.status_code == 403


# ── admin_usage router ────────────────────────────────────────────────────────


def _make_admin_usage_app() -> FastAPI:
    from src.settings import get_settings
    get_settings.cache_clear()
    from src.gateway.routes.admin_usage import router
    from src.db.engine import get_session

    app = FastAPI()
    app.include_router(router, prefix="/admin")

    async def _mock_db() -> AsyncGenerator[MagicMock, None]:
        yield MagicMock()

    app.dependency_overrides[get_session] = _mock_db
    return app


@pytest.mark.asyncio
async def test_usage_summary_no_auth_returns_403() -> None:
    app = _make_admin_usage_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/admin/usage/summary?range=7d")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_usage_summary_invalid_range_returns_400() -> None:
    app = _make_admin_usage_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/admin/usage/summary?range=1h", headers=_ADMIN_HEADERS)
    assert resp.status_code == 400
    assert "range" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_usage_summary_valid_range_returns_200() -> None:
    with patch("src.gateway.routes.admin_usage._query_daily_series", new=AsyncMock(return_value=[])):
        app = _make_admin_usage_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/usage/summary?range=7d", headers=_ADMIN_HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_usage_summary_invalid_user_id_returns_400() -> None:
    app = _make_admin_usage_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/admin/usage/summary?range=7d&user_id=not-a-uuid",
            headers=_ADMIN_HEADERS,
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_usage_by_key_no_auth_returns_403() -> None:
    app = _make_admin_usage_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/admin/usage/by-key/{uuid.uuid4()}?range=7d")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_usage_by_key_invalid_range_returns_400() -> None:
    app = _make_admin_usage_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            f"/admin/usage/by-key/{uuid.uuid4()}?range=bad",
            headers=_ADMIN_HEADERS,
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_usage_by_key_valid_range_returns_200() -> None:
    with patch("src.gateway.routes.admin_usage._query_daily_series", new=AsyncMock(return_value=[])):
        app = _make_admin_usage_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                f"/admin/usage/by-key/{uuid.uuid4()}?range=30d",
                headers=_ADMIN_HEADERS,
            )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_usage_by_key_returns_rows_when_key_matches() -> None:
    """by-key endpoint forwards correct api_key_id filter to _query_daily_series."""
    key_id = uuid.uuid4()
    expected = [{"day": "2026-04-25", "requests": 3, "tokens": 450}]

    captured_kwargs: dict[str, Any] = {}

    async def _mock_query(session: Any, since: Any, **kwargs: Any) -> list[Any]:
        captured_kwargs.update(kwargs)
        if kwargs.get("api_key_id") == key_id:
            from src.gateway.routes.admin_usage import DailyUsage
            return [DailyUsage(day="2026-04-25", requests=3, tokens=450)]
        return []

    with patch("src.gateway.routes.admin_usage._query_daily_series", new=_mock_query):
        app = _make_admin_usage_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                f"/admin/usage/by-key/{key_id}?range=7d",
                headers=_ADMIN_HEADERS,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["requests"] == 3
    assert body[0]["tokens"] == 450
    assert captured_kwargs.get("api_key_id") == key_id


@pytest.mark.asyncio
async def test_usage_by_key_returns_empty_when_key_not_matching() -> None:
    """_query_daily_series filters by api_key_id; non-matching key returns []."""
    other_key_id = uuid.uuid4()

    async def _mock_query(session: Any, since: Any, **kwargs: Any) -> list[Any]:
        # Simulate: no jobs for this key
        return []

    with patch("src.gateway.routes.admin_usage._query_daily_series", new=_mock_query):
        app = _make_admin_usage_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(
                f"/admin/usage/by-key/{other_key_id}?range=7d",
                headers=_ADMIN_HEADERS,
            )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_usage_summary_returns_real_token_sums() -> None:
    """summary endpoint passes token sums through (not always 0)."""
    from src.gateway.routes.admin_usage import DailyUsage

    fake_data = [DailyUsage(day="2026-04-28", requests=5, tokens=1200)]

    with patch("src.gateway.routes.admin_usage._query_daily_series", new=AsyncMock(return_value=fake_data)):
        app = _make_admin_usage_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/usage/summary?range=7d", headers=_ADMIN_HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["tokens"] == 1200
    assert body[0]["requests"] == 5


# ── users_page_routes (UI) ────────────────────────────────────────────────────


def _make_ui_app(mock_session: MagicMock) -> FastAPI:
    """Minimal app with users page sub-router; injects mock session, no auth guard."""
    from src.settings import get_settings
    get_settings.cache_clear()
    from src.admin_ui.users_page_routes import router
    from src.db.engine import get_session

    app = FastAPI()
    app.include_router(router, prefix="/admin/ui")

    async def _override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest.mark.asyncio
async def test_users_page_renders_200() -> None:
    from src.db.crud.users import UserAggregate

    uid = uuid.uuid4()
    mock_agg = UserAggregate(
        id=uid,
        email="bob@example.com",
        created_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        key_count=1,
        current_month_requests=10,
        current_month_tokens=200,
    )
    mock_session = MagicMock()

    with patch(
        "src.admin_ui.users_page_routes.list_with_aggregates",
        new=AsyncMock(return_value=([mock_agg], 1)),
    ):
        app = _make_ui_app(mock_session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/ui/users")

    assert resp.status_code == 200
    assert "bob@example.com" in resp.text


@pytest.mark.asyncio
async def test_user_detail_not_found_returns_404() -> None:
    uid = uuid.uuid4()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    app = _make_ui_app(mock_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/admin/ui/users/{uid}")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_chart_data_invalid_range_returns_400() -> None:
    uid = uuid.uuid4()
    mock_session = MagicMock()

    app = _make_ui_app(mock_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/admin/ui/users/{uid}/_chart_data?range=bad")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_user_chart_data_valid_range_returns_json() -> None:
    uid = uuid.uuid4()

    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    app = _make_ui_app(mock_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(f"/admin/ui/users/{uid}/_chart_data?range=30d")

    assert resp.status_code == 200
    body = resp.json()
    assert "labels" in body
    assert "requests" in body
    assert isinstance(body["labels"], list)
