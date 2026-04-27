"""
Tests for POST /admin/api-keys/{id}/rotate.

Covers:
  - Rotate returns new plaintext + new prefix (200)
  - 404 for unknown key_id
  - 403 without admin token
  - Old prefix replaced in returned response
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.gateway.routes.admin_api_keys import router

_ADMIN_TOKEN = "test-admin-token-rotate"


@pytest.fixture()
def admin_client(monkeypatch):
    """TestClient with deterministic admin token (bypasses lru_cache pollution)."""
    from src.settings import get_settings

    settings = get_settings()
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "admin_token", SecretStr(_ADMIN_TOKEN))

    app = FastAPI()
    app.include_router(router, prefix="/admin")
    return TestClient(app)


def _make_key(prefix: str = "cwk_oldprefix") -> MagicMock:
    key = MagicMock()
    key.id = uuid4()
    key.prefix = prefix
    key.tier = "free"
    key.key_hash = "oldhash"
    key.name = "test key"
    key.user_id = uuid4()
    key.revoked_at = None
    key.last_used_at = None
    key.created_at = datetime.now(UTC)
    return key


def test_rotate_returns_new_key_200(admin_client):
    existing = _make_key("cwk_oldprefix")
    updated = _make_key("cwk_newprefix")
    updated.id = existing.id

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    with (
        patch(
            "src.gateway.routes.admin_api_keys.api_keys_crud.get_by_id",
            new_callable=AsyncMock,
            side_effect=[existing, updated],
        ),
        patch(
            "src.gateway.routes.admin_api_keys.generate_api_key",
            return_value=("cwk_newplaintext_abc123", "cwk_newprefix", "newhash"),
        ),
        patch("src.db.engine._main_session_factory", return_value=mock_session),
    ):
        resp = admin_client.post(
            f"/admin/api-keys/{existing.id}/rotate",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "key" in body
    assert body["prefix"] == "cwk_newprefix"
    assert body["id"] == str(existing.id)


def test_rotate_404_for_unknown_key(admin_client):
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()

    with (
        patch(
            "src.gateway.routes.admin_api_keys.api_keys_crud.get_by_id",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("src.db.engine._main_session_factory", return_value=mock_session),
    ):
        resp = admin_client.post(
            f"/admin/api-keys/{uuid4()}/rotate",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "api_key_not_found"


def test_rotate_403_without_admin_token(admin_client):
    resp = admin_client.post(f"/admin/api-keys/{uuid4()}/rotate")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "permission_denied"


def test_rotate_403_wrong_token(admin_client):
    resp = admin_client.post(
        f"/admin/api-keys/{uuid4()}/rotate",
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert resp.status_code == 403
