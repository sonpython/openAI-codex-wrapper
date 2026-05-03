"""
Unit tests for /admin/api-keys endpoints.

NOTE: This file intentionally omits `from __future__ import annotations`.
FastAPI resolves route parameter annotations eagerly; PEP 563 lazy evaluation
turns them into forward-reference strings that pydantic cannot resolve for
Starlette/FastAPI types.

Uses FastAPI dependency_overrides to inject mock DB sessions — no real DB.

Covers:
  - POST /admin/api-keys: missing X-Admin-Token -> 403
  - POST /admin/api-keys: wrong X-Admin-Token -> 403
  - POST /admin/api-keys: correct token -> 201, plaintext key in response
  - POST /admin/api-keys: plaintext starts with cwk_
  - POST /admin/api-keys: invalid tier -> 422
  - POST /admin/api-keys: blank name -> 422
  - GET /admin/api-keys: wrong token -> 403
  - GET /admin/api-keys: correct token -> 200, list without key_hash
  - DELETE /admin/api-keys/{id}: wrong token -> 403
  - DELETE /admin/api-keys/{id}: found -> 204
  - DELETE /admin/api-keys/{id}: not found -> 404
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")

_ADMIN_TOKEN = "test-admin-secret"
_WRONG_TOKEN = "wrong-token"


def _mock_api_key_row(
    *, tier: str = "free", mode: str = "sandbox", revoked: bool = False
) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.user_id = uuid4()
    row.prefix = "cwk_testprefi"
    row.name = "test key"
    row.tier = tier
    row.mode = mode
    row.last_used_at = None
    row.revoked_at = datetime.now() if revoked else None  # noqa: DTZ005 — test only
    row.created_at = datetime.now()  # noqa: DTZ005 — test only
    return row


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


def _make_app_with_session(mock_session: MagicMock) -> object:
    from fastapi import FastAPI
    from src.db.engine import get_session
    from src.gateway.routes.admin_api_keys import router
    from src.settings import get_settings

    # Clear lru_cache so Settings re-reads ADMIN_TOKEN from os.environ above.
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
    app = _make_app_with_session(mock_session)
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        yield ac


# ── Admin token guard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_key_missing_admin_token_returns_403(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/api-keys",
        json={"user_email": "a@b.com", "name": "test"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_key_wrong_admin_token_returns_403(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/api-keys",
        json={"user_email": "a@b.com", "name": "test"},
        headers={"X-Admin-Token": _WRONG_TOKEN},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_list_keys_wrong_token_returns_403(client: AsyncClient) -> None:
    response = await client.get(
        "/admin/api-keys",
        headers={"X-Admin-Token": _WRONG_TOKEN},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_revoke_wrong_token_returns_403(client: AsyncClient) -> None:
    response = await client.delete(
        f"/admin/api-keys/{uuid4()}",
        headers={"X-Admin-Token": _WRONG_TOKEN},
    )
    assert response.status_code == 403


# ── POST /admin/api-keys ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_key_returns_201_with_plaintext(client: AsyncClient) -> None:
    mock_key = _mock_api_key_row()
    plaintext = "cwk_" + "F" * 43

    with (
        patch(
            "src.gateway.routes.admin_api_keys.get_or_create_by_email",
            new=AsyncMock(return_value=(MagicMock(id=mock_key.user_id), True)),
        ),
        patch(
            "src.gateway.routes.admin_api_keys.api_keys_crud.create",
            new=AsyncMock(return_value=(mock_key, plaintext)),
        ),
    ):
        response = await client.post(
            "/admin/api-keys",
            json={"user_email": "a@b.com", "name": "smoke", "tier": "free"},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["key"] == plaintext
    assert body["key"].startswith("cwk_")
    assert "id" in body
    assert "prefix" in body
    assert "tier" in body


@pytest.mark.asyncio
async def test_create_key_invalid_tier_returns_422(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/api-keys",
        json={"user_email": "a@b.com", "name": "test", "tier": "enterprise"},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_key_blank_name_returns_422(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/api-keys",
        json={"user_email": "a@b.com", "name": "   "},
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )
    assert response.status_code == 422


# ── GET /admin/api-keys ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_keys_returns_200_with_summaries(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    mock_key = _mock_api_key_row()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_key]
    mock_session.execute = AsyncMock(return_value=mock_result)

    response = await client.get(
        "/admin/api-keys",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    item = body[0]
    assert "prefix" in item
    assert "key_hash" not in item  # never exposed
    assert "key" not in item  # plaintext never returned after creation


# ── DELETE /admin/api-keys/{id} ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_existing_key_returns_204(client: AsyncClient) -> None:
    key_id = uuid4()

    with patch(
        "src.gateway.routes.admin_api_keys.api_keys_crud.revoke",
        new=AsyncMock(return_value=True),
    ):
        response = await client.delete(
            f"/admin/api-keys/{key_id}",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_revoke_nonexistent_key_returns_404(client: AsyncClient) -> None:
    key_id = uuid4()

    with patch(
        "src.gateway.routes.admin_api_keys.api_keys_crud.revoke",
        new=AsyncMock(return_value=False),
    ):
        response = await client.delete(
            f"/admin/api-keys/{key_id}",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert response.status_code == 404


# ── Mode field ────────────────────────────────────────────────────────────────


class TestModeField:
    """POST /admin/api-keys mode validation and persistence."""

    @pytest.mark.asyncio
    async def test_create_key_with_mode_vps_persists_mode(self, client: AsyncClient) -> None:
        mock_key = _mock_api_key_row(mode="vps")
        plaintext = "cwk_" + "V" * 43

        with (
            patch(
                "src.gateway.routes.admin_api_keys.get_or_create_by_email",
                new=AsyncMock(return_value=(MagicMock(id=mock_key.user_id), False)),
            ),
            patch(
                "src.gateway.routes.admin_api_keys.api_keys_crud.create",
                new=AsyncMock(return_value=(mock_key, plaintext)),
            ),
        ):
            response = await client.post(
                "/admin/api-keys",
                json={"user_email": "a@b.com", "name": "vps-key", "tier": "pro", "mode": "vps"},
                headers={"X-Admin-Token": _ADMIN_TOKEN},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["mode"] == "vps"

    @pytest.mark.asyncio
    async def test_create_key_without_mode_defaults_to_sandbox(self, client: AsyncClient) -> None:
        mock_key = _mock_api_key_row(mode="sandbox")
        plaintext = "cwk_" + "S" * 43

        with (
            patch(
                "src.gateway.routes.admin_api_keys.get_or_create_by_email",
                new=AsyncMock(return_value=(MagicMock(id=mock_key.user_id), False)),
            ),
            patch(
                "src.gateway.routes.admin_api_keys.api_keys_crud.create",
                new=AsyncMock(return_value=(mock_key, plaintext)),
            ),
        ):
            response = await client.post(
                "/admin/api-keys",
                json={"user_email": "a@b.com", "name": "default-key", "tier": "free"},
                headers={"X-Admin-Token": _ADMIN_TOKEN},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["mode"] == "sandbox"

    @pytest.mark.asyncio
    async def test_create_key_invalid_mode_returns_422(self, client: AsyncClient) -> None:
        response = await client.post(
            "/admin/api-keys",
            json={"user_email": "a@b.com", "name": "bad-key", "tier": "free", "mode": "bogus"},
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )
        assert response.status_code == 422
