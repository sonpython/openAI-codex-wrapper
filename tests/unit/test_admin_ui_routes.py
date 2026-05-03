"""
Unit tests for src/admin_ui/routes.py

Covers:
  - GET /admin/ui/login renders 200
  - POST /admin/ui/login with valid token sets cookie + redirects
  - POST /admin/ui/login with invalid token returns 401 + error message
  - GET /admin/ui/logout clears cookie + redirects to login
  - GET /admin/ui/ without session redirects to login (302)
  - GET /admin/ui/ without session via HTMX returns HX-Redirect header
  - GET /admin/ui/ with valid session returns 200 dashboard HTML
  - GET /admin/ui/partials/kpis without HTMX header redirects to dashboard
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.admin_ui.routes import (
    _COOKIE_NAME,
    _SESSION_REQUIRED_DETAIL,
    make_session_redirect_response,
    router,
)

# ── App fixture ────────────────────────────────────────────────────────────────

_TOKEN = "test-secret-token-xyz"
_VALID_SID = "valid-session-id-abc"
_SIGNED_COOKIE = None  # set after import


def _make_app() -> FastAPI:
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(HTTPException)
    async def _exc(request, exc):  # type: ignore[no-untyped-def]

        if exc.status_code == 401 and exc.detail == _SESSION_REQUIRED_DETAIL:
            return make_session_redirect_response(request)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return _make_app()


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _patch_settings(token: str = _TOKEN):  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    s = MagicMock()
    s.admin_token = SecretStr(token)
    s.admin_session_ttl_seconds = 28800
    s.wrapper_env = "dev"
    s.prometheus_url = None
    s.internal_metrics_path = "/_internal/metrics"
    return patch("src.admin_ui.routes.get_settings", return_value=s)


def _patch_redis(sid_exists: bool = True):  # type: ignore[no-untyped-def]
    redis_mock = AsyncMock()
    redis_mock.set = AsyncMock()
    redis_mock.exists = AsyncMock(return_value=1 if sid_exists else 0)
    redis_mock.delete = AsyncMock()
    return patch("src.admin_ui.routes.get_client", return_value=redis_mock), redis_mock


def _valid_cookie(token: str = _TOKEN) -> str:
    from src.admin_ui.auth import sign_session
    return sign_session(_VALID_SID, token)


# ── Login page ────────────────────────────────────────────────────────────────


def test_get_login_returns_200(client: TestClient) -> None:
    resp = client.get("/admin/ui/login")
    assert resp.status_code == 200
    assert b"Admin Token" in resp.content or b"admin" in resp.content.lower()


# ── POST /login ────────────────────────────────────────────────────────────────


def test_post_login_valid_token_sets_cookie_and_redirects(client: TestClient) -> None:
    redis_patch, redis_mock = _patch_redis()
    with _patch_settings(), redis_patch:
        redis_mock.set = AsyncMock(return_value=True)
        resp = client.post(
            "/admin/ui/login",
            data={"token": _TOKEN},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert resp.headers.get("location", "").endswith("/admin/ui/")
    assert _COOKIE_NAME in resp.cookies


def test_post_login_invalid_token_returns_401(client: TestClient) -> None:
    redis_patch, _ = _patch_redis()
    with _patch_settings(), redis_patch:
        resp = client.post(
            "/admin/ui/login",
            data={"token": "wrong-token"},
        )
    assert resp.status_code == 401
    assert b"Invalid token" in resp.content


def test_post_login_missing_token_returns_422(client: TestClient) -> None:
    resp = client.post("/admin/ui/login", data={})
    assert resp.status_code == 422


# ── GET /logout ────────────────────────────────────────────────────────────────


def test_get_logout_clears_cookie_and_redirects(client: TestClient) -> None:
    redis_patch, redis_mock = _patch_redis()
    with _patch_settings(), redis_patch:
        cookie_val = _valid_cookie()
        resp = client.get(
            "/admin/ui/logout",
            cookies={_COOKIE_NAME: cookie_val},
            follow_redirects=False,
        )
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")
    # Cookie should be cleared (max-age=0 or deleted)
    set_cookie = resp.headers.get("set-cookie", "")
    assert _COOKIE_NAME in set_cookie


# ── Dashboard auth guard ───────────────────────────────────────────────────────


def test_dashboard_without_session_redirects_to_login(client: TestClient) -> None:
    redis_patch, _ = _patch_redis(sid_exists=False)
    with _patch_settings(), redis_patch:
        resp = client.get("/admin/ui/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")


def test_dashboard_htmx_without_session_returns_hx_redirect(client: TestClient) -> None:
    redis_patch, _ = _patch_redis(sid_exists=False)
    with _patch_settings(), redis_patch:
        resp = client.get(
            "/admin/ui/",
            headers={"HX-Request": "true"},
            follow_redirects=False,
        )
    assert resp.status_code == 204
    assert resp.headers.get("HX-Redirect", "").endswith("/admin/ui/login")


def test_dashboard_with_valid_session_returns_200(client: TestClient) -> None:
    redis_patch, _ = _patch_redis(sid_exists=True)
    kpi_patch = patch(
        "src.admin_ui.routes.fetch_kpis",
        new=AsyncMock(return_value=MagicMock(
            req_rate_1m=1.0, error_rate_5m=0.0,
            active_jobs=0.0, queue_depth=0.0,
        )),
    )
    spark_patch = patch(
        "src.admin_ui.routes.fetch_sparklines",
        new=AsyncMock(return_value=MagicMock(req_24h=[], error_24h=[])),
    )
    with _patch_settings(), redis_patch, kpi_patch, spark_patch:
        cookie_val = _valid_cookie()
        resp = client.get(
            "/admin/ui/",
            cookies={_COOKIE_NAME: cookie_val},
        )
    assert resp.status_code == 200
    assert b"Dashboard" in resp.content or b"dashboard" in resp.content.lower()


# ── KPI partial ────────────────────────────────────────────────────────────────


def test_kpis_partial_without_htmx_header_redirects(client: TestClient) -> None:
    redis_patch, _ = _patch_redis(sid_exists=True)
    with _patch_settings(), redis_patch:
        cookie_val = _valid_cookie()
        resp = client.get(
            "/admin/ui/partials/kpis",
            cookies={_COOKIE_NAME: cookie_val},
            follow_redirects=False,
        )
    assert resp.status_code == 302
