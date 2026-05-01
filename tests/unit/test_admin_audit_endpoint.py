"""
Unit tests for GET /admin/audit endpoint.

Covers:
  - Missing X-Admin-Token → 403
  - Wrong X-Admin-Token → 403
  - Correct token, no entries → 200 empty list
  - Correct token, with entries → 200 items returned
  - limit > 500 → clamped to 500
  - action filter forwarded to crud
  - user_id filter forwarded to crud
  - Pagination: offset forwarded correctly
  - CRUD exception → 500
  - detail field serialised as dict in response
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime, UTC
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")

_TOKEN = "test-admin-secret"
_WRONG = "bad-token"


def _make_entry(**kwargs):
    now = datetime.now(UTC)
    base = {
        "id": 1,
        "created_at": now,
        "actor_email": None,
        "action": "create",
        "target": str(uuid4()),
        "ip": None,
        "status": 201,
        "detail": {
            "request_id": "req-abc",
            "route": "/admin/api-keys",
            "method": "POST",
            "duration_ms": 50,
            "user_id": None,
            "api_key_id": None,
            "admin": True,
            "prompt_hash": None,
            "input_tokens": None,
            "output_tokens": None,
            "error_class": None,
        },
    }
    base.update(kwargs)
    return base


def _make_app():
    from fastapi import FastAPI
    from src.db.engine import get_session
    from src.gateway.routes.admin_audit import router
    from src.settings import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(router, prefix="/admin")

    mock_session = AsyncMock()

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_session] = _override
    return app


@pytest.mark.anyio
async def test_audit_missing_token():
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/audit")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_audit_wrong_token():
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/audit", headers={"X-Admin-Token": _WRONG})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_audit_empty_list():
    app = _make_app()
    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=AsyncMock(return_value=([], 0))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0


@pytest.mark.anyio
async def test_audit_returns_items():
    entry = _make_entry()
    app = _make_app()
    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=AsyncMock(return_value=([entry], 1))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["total"] == 1
    assert body["items"][0]["action"] == "create"
    assert body["items"][0]["status"] == 201
    assert isinstance(body["items"][0]["detail"], dict)


@pytest.mark.anyio
async def test_audit_limit_clamped_to_500():
    app = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit?limit=9999", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["limit"] == 500


@pytest.mark.anyio
async def test_audit_action_filter_forwarded():
    app = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit?action=rotate", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["action"] == "rotate"


@pytest.mark.anyio
async def test_audit_user_id_filter_forwarded():
    uid = uuid4()
    app = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/admin/audit?user_id={uid}", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["user_id"] == uid


@pytest.mark.anyio
async def test_audit_offset_forwarded():
    app = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 200

    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit?offset=100", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["offset"] == 100
    assert resp.json()["offset"] == 100


@pytest.mark.anyio
async def test_audit_crud_exception_returns_500():
    app = _make_app()

    async def _raise(session, **kwargs):
        raise RuntimeError("db error")

    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=_raise):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 500


@pytest.mark.anyio
async def test_audit_detail_null_is_valid():
    """Entry with detail=None serialises correctly (no crash)."""
    entry = _make_entry(detail=None)
    app = _make_app()
    with patch("src.gateway.routes.admin_audit.audit_crud.list_with_filters", new=AsyncMock(return_value=([entry], 1))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/audit", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["detail"] is None
