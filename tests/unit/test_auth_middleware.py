"""
Unit tests for src/gateway/middleware/auth.py.

NOTE: This file intentionally omits `from __future__ import annotations`.
FastAPI resolves route parameter annotations at class-definition time; the
PEP 563 lazy evaluation (future annotations) causes pydantic to receive
forward-reference strings it cannot resolve for Starlette types like Request.

Covers:
  - Missing Authorization header -> 401 with OpenAI error shape
  - Wrong scheme (Basic, Token) -> 401
  - Non-cwk_ prefixed token -> 401
  - Valid token, DB returns None (unknown key) -> 401
  - Valid token, DB returns active key -> 200 + state populated
  - Health/readyz paths bypass auth entirely -> 200
  - Malformed Authorization (no space) -> 401
  - Error body shape matches OpenAI spec exactly
  - fire-and-forget called on success
"""

import os
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _mock_api_key(tier: str = "free") -> MagicMock:
    key = MagicMock()
    key.id = uuid4()
    key.user_id = uuid4()
    key.tier = tier
    return key


def _make_test_app() -> object:
    """Build a minimal FastAPI app with AuthMiddleware + a protected /v1/ping route.

    Defined in a plain function (no future annotations import) so FastAPI can
    resolve Starlette's Request type annotation at route registration time.
    """
    from fastapi import FastAPI, Request
    from src.gateway.middleware.auth import AuthMiddleware

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/v1/ping")
    async def ping(request: Request) -> dict:  # type: ignore[type-arg]
        return {
            "pong": True,
            "user_id": str(request.state.user_id),
            "tier": str(request.state.tier),
        }

    @app.get("/healthz")
    async def healthz() -> dict:  # type: ignore[type-arg]
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict:  # type: ignore[type-arg]
        return {"status": "ok"}

    return app


@pytest_asyncio.fixture()
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        yield ac


# ── Bypass paths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_bypasses_auth(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_readyz_bypasses_auth(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200


# ── Missing / malformed Authorization ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/ping")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_basic_scheme_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/ping", headers={"Authorization": "Basic abc123"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_token_scheme_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/ping", headers={"Authorization": "Token cwk_abc"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_non_cwk_prefix_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/ping", headers={"Authorization": "Bearer sk-proj-abc123"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_malformed_no_space_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/ping", headers={"Authorization": "Bearercwk_abc"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_empty_bearer_value_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/ping", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


# ── Error body shape ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_401_body_matches_openai_shape(client: AsyncClient) -> None:
    response = await client.get("/v1/ping")
    body = response.json()
    assert "error" in body
    err = body["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "invalid_api_key"
    assert "message" in err
    assert "param" in err  # present even when null


# ── DB lookup paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_key_returns_401(client: AsyncClient) -> None:
    """Valid cwk_ token structure but not found in DB -> 401."""
    valid_token = "cwk_" + "A" * 43

    with patch(
        "src.gateway.middleware.auth.AuthMiddleware._authenticate",
        new=AsyncMock(return_value=None),
    ):
        response = await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_key_returns_200_and_populates_state(client: AsyncClient) -> None:
    """Valid cwk_ token found in DB -> 200 with user_id + tier in response."""
    valid_token = "cwk_" + "B" * 43
    mock_key = _mock_api_key(tier="pro")

    with (
        patch(
            "src.gateway.middleware.auth.AuthMiddleware._authenticate",
            new=AsyncMock(return_value=mock_key),
        ),
        patch("src.gateway.middleware.auth.update_last_used_fire_and_forget") as mock_bg,
    ):
        response = await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["pong"] is True
    assert body["tier"] == "pro"
    assert body["user_id"] == str(mock_key.user_id)
    mock_bg.assert_called_once_with(mock_key.id)


@pytest.mark.asyncio
async def test_fire_and_forget_called_on_success(client: AsyncClient) -> None:
    valid_token = "cwk_" + "C" * 43
    mock_key = _mock_api_key()

    with (
        patch(
            "src.gateway.middleware.auth.AuthMiddleware._authenticate",
            new=AsyncMock(return_value=mock_key),
        ),
        patch("src.gateway.middleware.auth.update_last_used_fire_and_forget") as mock_bg,
    ):
        await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})
        mock_bg.assert_called_once()
