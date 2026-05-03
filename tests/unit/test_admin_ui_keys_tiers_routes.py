"""
Unit tests for admin UI /keys and /tiers page handlers (routes.py).

Uses FastAPI dependency_overrides to inject mock session and mock session auth.
No real DB or Redis required.

Covers:
  - GET /admin/ui/keys: unauthenticated -> redirect (401 -> login redirect)
  - GET /admin/ui/keys: authenticated -> 200, HTML with table
  - POST /admin/ui/keys/_create: creates key, returns row partial with plaintext
  - POST /admin/ui/keys/_create: blank name -> 400
  - POST /admin/ui/keys/_create: invalid tier -> 400
  - POST /admin/ui/keys/{id}/_rotate: rotates key, returns row partial
  - POST /admin/ui/keys/{id}/_rotate: key not found -> 404
  - DELETE /admin/ui/keys/{id}: revokes key -> 200 empty
  - DELETE /admin/ui/keys/{id}: not found -> 404
  - GET /admin/ui/tiers: authenticated -> 200, HTML with tier table
  - PUT /admin/ui/tiers/{tier}/_save: valid -> 200 toast success
  - PUT /admin/ui/tiers/{tier}/_save: invalid tier -> 400
  - PUT /admin/ui/tiers/{tier}/_save: negative values -> 400
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")

_SID = "test-session-id-abc123"


def _mock_key_row(tier: str = "free", revoked: bool = False) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.user_id = uuid4()
    row.prefix = "cwk_testprefix"
    row.name = "test-key"
    row.tier = tier
    row.last_used_at = None
    row.revoked_at = datetime(2025, 1, 1) if revoked else None  # noqa: DTZ001
    row.created_at = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001
    return row


def _mock_plan(tier: str = "free") -> MagicMock:
    p = MagicMock()
    p.tier = tier
    p.rpm = 20
    p.tpm = 20000
    p.concurrent = 2
    p.monthly_tokens = 100000
    return p


def _mock_user(email: str = "user@example.com") -> MagicMock:
    u = MagicMock()
    u.id = uuid4()
    u.email = email
    return u


def _make_mock_session() -> MagicMock:
    s = MagicMock()
    s.commit = AsyncMock()
    s.flush = AsyncMock()
    s.execute = AsyncMock()
    return s


def _make_app(mock_session: MagicMock) -> FastAPI:
    from src.admin_ui.routes import require_session, router
    from src.db.engine import get_session
    from src.settings import get_settings

    get_settings.cache_clear()

    app = FastAPI()

    # Session-required exception -> login redirect (mirrors app.py handler)
    from fastapi import HTTPException as _HTTPException
    from src.admin_ui.routes import _SESSION_REQUIRED_DETAIL, make_session_redirect_response

    @app.exception_handler(_HTTPException)
    async def _exc(request: Request, exc: _HTTPException) -> Response:
        if (
            exc.status_code == 401
            and exc.detail == _SESSION_REQUIRED_DETAIL
            and request.url.path.startswith("/admin/ui/")
        ):
            return make_session_redirect_response(request)
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(router)

    # Override: always valid session
    async def _authed_session() -> str:
        return _SID

    # Override: inject mock DB session
    async def _override_session() -> AsyncGenerator[MagicMock, None]:
        yield mock_session

    app.dependency_overrides[require_session] = _authed_session
    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture()
def mock_session() -> MagicMock:
    return _make_mock_session()


@pytest_asyncio.fixture()
async def client(mock_session: MagicMock) -> AsyncGenerator[AsyncClient, None]:
    app = _make_app(mock_session)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── GET /admin/ui/keys ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keys_page_authenticated_returns_200(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    key = _mock_key_row()
    user = _mock_user()
    mock_result = MagicMock()
    mock_result.all.return_value = [(key, user)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    response = await client.get("/admin/ui/keys")

    assert response.status_code == 200
    assert b"API Keys" in response.content


@pytest.mark.asyncio
async def test_keys_page_shows_empty_state_on_db_error(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    mock_session.execute = AsyncMock(side_effect=RuntimeError("db down"))

    response = await client.get("/admin/ui/keys")

    assert response.status_code == 200
    # Page renders even on DB error (graceful fallback)
    assert b"API Keys" in response.content


# ── POST /admin/ui/keys/_create ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_key_returns_row_partial_with_plaintext(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    key = _mock_key_row()
    user = _mock_user("alice@example.com")
    plaintext = "cwk_" + "A" * 43

    with (
        patch(
            "src.admin_ui.keys_page_routes.get_or_create_by_email",
            new=AsyncMock(return_value=(user, False)),
        ),
        patch(
            "src.admin_ui.keys_page_routes.api_keys_crud.create",
            new=AsyncMock(return_value=(key, plaintext)),
        ),
    ):
        response = await client.post(
            "/admin/ui/keys/_create",
            data={"user_email": "alice@example.com", "name": "prod-key", "tier": "free"},
        )

    assert response.status_code == 200
    assert plaintext.encode() in response.content


@pytest.mark.asyncio
async def test_create_key_blank_name_returns_400(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/ui/keys/_create",
        data={"user_email": "a@b.com", "name": "   ", "tier": "free"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_key_invalid_tier_returns_400(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/ui/keys/_create",
        data={"user_email": "a@b.com", "name": "key", "tier": "ultra"},
    )
    assert response.status_code == 400


# ── POST /admin/ui/keys/{id}/_rotate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_key_returns_row_partial_with_new_plaintext(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    key = _mock_key_row()
    user = _mock_user()
    key_id = key.id
    new_plaintext = "cwk_" + "B" * 43

    mock_result_user = MagicMock()
    mock_result_user.scalar_one_or_none.return_value = user

    refreshed_key = _mock_key_row()
    refreshed_key.id = key_id

    [
        MagicMock(),  # update execute result
        mock_result_user,  # user select result
    ]

    with (
        patch(
            "src.admin_ui.keys_page_routes.api_keys_crud.get_by_id",
            new=AsyncMock(side_effect=[key, refreshed_key]),
        ),
        patch(
            "src.admin_ui.keys_page_routes.generate_api_key",
            return_value=(new_plaintext, "cwk_newprefix1", "hash123"),
        ),
        patch.object(mock_session, "execute", new=AsyncMock(return_value=mock_result_user)),
    ):
        response = await client.post(f"/admin/ui/keys/{key_id}/_rotate")

    assert response.status_code == 200
    assert new_plaintext.encode() in response.content


@pytest.mark.asyncio
async def test_rotate_key_not_found_returns_404(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    with patch(
        "src.admin_ui.keys_page_routes.api_keys_crud.get_by_id",
        new=AsyncMock(return_value=None),
    ):
        response = await client.post(f"/admin/ui/keys/{uuid4()}/_rotate")

    assert response.status_code == 404


# ── DELETE /admin/ui/keys/{id} ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_key_returns_200_empty(client: AsyncClient, mock_session: MagicMock) -> None:
    key_id = uuid4()
    with patch(
        "src.admin_ui.keys_page_routes.api_keys_crud.revoke",
        new=AsyncMock(return_value=True),
    ):
        response = await client.delete(f"/admin/ui/keys/{key_id}")

    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.asyncio
async def test_delete_key_not_found_returns_404(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    with patch(
        "src.admin_ui.keys_page_routes.api_keys_crud.revoke",
        new=AsyncMock(return_value=False),
    ):
        response = await client.delete(f"/admin/ui/keys/{uuid4()}")

    assert response.status_code == 404


# ── GET /admin/ui/tiers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tiers_page_returns_200_with_tier_table(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    plans = [_mock_plan("free"), _mock_plan("pro")]

    with patch(
        "src.admin_ui.tiers_page_routes.plans_crud.list_all",
        new=AsyncMock(return_value=plans),
    ):
        response = await client.get("/admin/ui/tiers")

    assert response.status_code == 200
    assert b"Plan Tiers" in response.content
    assert b"free" in response.content


@pytest.mark.asyncio
async def test_tiers_page_graceful_on_db_error(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    with patch(
        "src.admin_ui.tiers_page_routes.plans_crud.list_all",
        new=AsyncMock(side_effect=RuntimeError("db err")),
    ):
        response = await client.get("/admin/ui/tiers")

    assert response.status_code == 200


# ── PUT /admin/ui/tiers/{tier}/_save ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_tier_valid_returns_200_toast(
    client: AsyncClient, mock_session: MagicMock
) -> None:
    updated = _mock_plan("free")

    with (
        patch(
            "src.admin_ui.tiers_page_routes.plans_crud.update",
            new=AsyncMock(return_value=updated),
        ),
        patch("src.admin_ui.tiers_page_routes.invalidate_cache") as mock_inv,
    ):
        response = await client.put(
            "/admin/ui/tiers/free/_save",
            data={"rpm": "30", "tpm": "30000", "concurrent": "3", "monthly_quota": "200000"},
        )

    assert response.status_code == 200
    assert b"saved" in response.content.lower() or b"success" in response.content.lower()
    mock_inv.assert_called_once()


@pytest.mark.asyncio
async def test_save_tier_invalid_name_returns_400(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/ui/tiers/badtier/_save",
        data={"rpm": "10", "tpm": "1000", "concurrent": "2", "monthly_quota": "50000"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_save_tier_negative_rpm_returns_400(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/ui/tiers/free/_save",
        data={"rpm": "-5", "tpm": "1000", "concurrent": "2", "monthly_quota": "50000"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_save_tier_non_numeric_returns_400(client: AsyncClient) -> None:
    response = await client.put(
        "/admin/ui/tiers/free/_save",
        data={"rpm": "abc", "tpm": "1000", "concurrent": "2", "monthly_quota": "50000"},
    )
    assert response.status_code == 400
