"""
Unit tests for GET /admin/jobs endpoint.

Covers:
  - Missing X-Admin-Token → 403
  - Wrong X-Admin-Token → 403
  - Correct token, no jobs → 200 empty list
  - Correct token, with jobs → 200 items returned
  - limit > 500 → clamped to 500
  - limit = 1 → 1 item returned
  - status filter forwarded to crud
  - user_id filter forwarded to crud
  - Pagination: offset forwarded correctly
  - CRUD exception → 500
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


def _make_job_item(**kwargs):
    now = datetime.now(UTC)
    base = {
        "id": str(uuid4()),
        "user_email": "user@example.com",
        "status": "succeeded",
        "model": "workspace-write",
        "created_at": now,
        "completed_at": now,
        "duration_ms": 1234,
        "exit_code": 0,
        "prompt_hash": None,
        "repo_url": "https://github.com/org/repo",
        "branch": "main",
        "error_code": None,
        "error_message": None,
        "stderr_tail": None,
    }
    base.update(kwargs)
    return base


def _make_app(mock_items=None, mock_total=0, raise_exc=False):
    from fastapi import FastAPI
    from src.db.engine import get_session
    from src.gateway.routes.admin_jobs import router
    from src.settings import get_settings

    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(router, prefix="/admin")

    mock_session = AsyncMock()

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_session] = _override

    if raise_exc:
        patch_target = "src.gateway.routes.admin_jobs.jobs_crud.list_with_filters"
        app.state._patch_target = patch_target  # store for test to use
    return app, mock_session, mock_items or [], mock_total


@pytest.mark.anyio
async def test_jobs_missing_token():
    app, _, _, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/jobs")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_jobs_wrong_token():
    app, _, _, _ = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/jobs", headers={"X-Admin-Token": _WRONG})
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_jobs_empty_list():
    app, _, items, total = _make_app(mock_items=[], mock_total=0)
    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=AsyncMock(return_value=(items, total))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 50
    assert body["offset"] == 0


@pytest.mark.anyio
async def test_jobs_returns_items():
    job = _make_job_item()
    app, _, _, _ = _make_app()
    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=AsyncMock(return_value=([job], 1))):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["total"] == 1
    assert body["items"][0]["status"] == "succeeded"
    assert body["items"][0]["user_email"] == "user@example.com"


@pytest.mark.anyio
async def test_jobs_limit_clamped_to_500():
    app, _, _, _ = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs?limit=1000", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["limit"] == 500


@pytest.mark.anyio
async def test_jobs_limit_minimum_1():
    app, _, _, _ = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # limit=0 is rejected by ge=1 constraint → 422; limit=1 is valid
            resp = await client.get("/admin/jobs?limit=1", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["limit"] == 1


@pytest.mark.anyio
async def test_jobs_status_filter_forwarded():
    app, _, _, _ = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs?status=failed", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["status"] == "failed"


@pytest.mark.anyio
async def test_jobs_user_id_filter_forwarded():
    uid = uuid4()
    app, _, _, _ = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 0

    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(f"/admin/jobs?user_id={uid}", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["user_id"] == uid


@pytest.mark.anyio
async def test_jobs_offset_forwarded():
    app, _, _, _ = _make_app()
    captured = {}

    async def _mock(session, **kwargs):
        captured.update(kwargs)
        return [], 100

    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=_mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs?offset=50", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 200
    assert captured["offset"] == 50
    assert resp.json()["offset"] == 50


@pytest.mark.anyio
async def test_jobs_crud_exception_returns_500():
    app, _, _, _ = _make_app()

    async def _raise(session, **kwargs):
        raise RuntimeError("db down")

    with patch("src.gateway.routes.admin_jobs.jobs_crud.list_with_filters", new=_raise):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/admin/jobs", headers={"X-Admin-Token": _TOKEN})
    assert resp.status_code == 500
