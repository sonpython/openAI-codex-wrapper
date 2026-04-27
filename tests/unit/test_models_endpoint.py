"""
Unit tests for GET /v1/models.

NOTE: This file intentionally omits `from __future__ import annotations` for
the same reason as test_auth_middleware.py — FastAPI resolves route annotations
eagerly and pydantic cannot handle forward-reference strings for Starlette types.

Covers:
  - 401 without auth (auth middleware rejects before route handler)
  - 200 with valid key
  - Response shape matches OpenAI list-models spec exactly
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


def _mock_api_key() -> MagicMock:
    key = MagicMock()
    key.id = uuid4()
    key.user_id = uuid4()
    key.tier = "free"
    return key


def _make_app() -> object:
    from fastapi import FastAPI
    from src.gateway.middleware.auth import AuthMiddleware
    from src.gateway.routes.models import router as models_router

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(models_router)

    return app


@pytest_asyncio.fixture()
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_models_without_auth_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_models_with_valid_key_returns_200(client: AsyncClient) -> None:
    mock_key = _mock_api_key()
    token = "cwk_" + "D" * 43

    with (
        patch(
            "src.gateway.middleware.auth.AuthMiddleware._authenticate",
            new=AsyncMock(return_value=mock_key),
        ),
        patch("src.gateway.middleware.auth.update_last_used_fire_and_forget"),
    ):
        response = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_models_response_shape(client: AsyncClient) -> None:
    mock_key = _mock_api_key()
    token = "cwk_" + "E" * 43

    with (
        patch(
            "src.gateway.middleware.auth.AuthMiddleware._authenticate",
            new=AsyncMock(return_value=mock_key),
        ),
        patch("src.gateway.middleware.auth.update_last_used_fire_and_forget"),
    ):
        response = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})

    body = response.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) == 1

    model = body["data"][0]
    assert model["id"] == "codex-cli"
    assert model["object"] == "model"
    assert model["owned_by"] == "codex-wrapper"
    assert isinstance(model["created"], int)
    assert model["created"] > 0
