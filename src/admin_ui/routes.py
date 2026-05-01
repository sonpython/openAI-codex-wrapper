"""
Admin UI page handlers — core auth, dashboard, and sub-router wiring.

Routes owned here (all under /admin/ui prefix):
  GET  /login          — render login form
  POST /login          — validate token, set signed cookie, redirect dashboard
  GET  /logout         — clear cookie + Redis session, redirect login
  GET  /               — dashboard (4 KPI cards + sparklines)
  GET  /partials/kpis  — HTMX partial, polled every 5s

Key/tier page handlers live in sub-modules included at the bottom:
  keys_page_routes.py  — /keys + /keys/_create + /keys/{id}/_rotate + /keys/{id}
  tiers_page_routes.py — /tiers + /tiers/{tier}/_save

Session guard
-------------
``require_session`` dependency validates the signed cookie + Redis presence.
On failure it raises HTTPException(401). The app-level exception handler
(registered in gateway/app.py) converts this to an HTMX-aware redirect:
  - HTMX request  → 204 + HX-Redirect header
  - Browser nav   → 302 to /admin/ui/login
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from src.admin_ui import auth as session_auth
from src.admin_ui.prom_client import KPISnapshot, SparklineData, fetch_kpis, fetch_sparklines
from src.admin_ui.templates_env import templates
from src.redis_client import get_client
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin/ui", tags=["admin-ui"])

_COOKIE_NAME = "admin_session"

# Sentinel used by app-level exception handler to distinguish session expiry
# from real 401s (e.g. wrong token on API endpoints).
_SESSION_REQUIRED_STATUS = 401
_SESSION_REQUIRED_DETAIL = "admin_session_required"


# ── Session dependency ─────────────────────────────────────────────────────────


async def require_session(
    request: Request,
    admin_session: Annotated[str | None, Cookie(alias=_COOKIE_NAME)] = None,
) -> str:
    """Return verified sid or raise HTTPException(401) with detail marker.

    The app-level exception handler converts this to an HTMX-aware redirect.
    """
    settings = get_settings()
    redis = get_client()

    sid: str | None = None
    if admin_session and redis is not None:
        sid = session_auth.verify_session(
            admin_session, settings.admin_token.get_secret_value()
        )
        if sid:
            valid = await session_auth.validate_session(redis, sid)
            if not valid:
                sid = None

    if sid is None:
        raise HTTPException(
            status_code=_SESSION_REQUIRED_STATUS,
            detail=_SESSION_REQUIRED_DETAIL,
        )

    return sid


def make_session_redirect_response(request: Request) -> Response:
    """Build HTMX-aware redirect to login page."""
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        return Response(status_code=204, headers={"HX-Redirect": "/admin/ui/login"})
    return RedirectResponse(url="/admin/ui/login", status_code=302)


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def get_login(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def post_login(
    request: Request,
    token: Annotated[str, Form()],
) -> Response:
    """Validate ADMIN_TOKEN; set signed HttpOnly cookie; redirect dashboard."""
    settings = get_settings()
    redis = get_client()

    expected = settings.admin_token.get_secret_value()
    if not secrets.compare_digest(token.encode(), expected.encode()):
        logger.warning("admin_ui.login_failed")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid token"},
            status_code=401,
        )

    if redis is None:
        logger.error("admin_ui.login.redis_unavailable")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Service temporarily unavailable"},
            status_code=503,
        )

    ttl = settings.admin_session_ttl_seconds
    sid = await session_auth.create_session(redis, ttl)
    cookie_value = session_auth.sign_session(sid, expected)

    resp = RedirectResponse(url="/admin/ui/", status_code=302)
    resp.set_cookie(
        key=_COOKIE_NAME,
        value=cookie_value,
        max_age=ttl,
        httponly=True,
        samesite="strict",
        secure=settings.wrapper_env == "prod",
    )
    logger.info("admin_ui.login_success", sid_prefix=sid[:8])
    return resp


@router.get("/logout")
async def get_logout(
    request: Request,
    admin_session: Annotated[str | None, Cookie(alias=_COOKIE_NAME)] = None,
) -> RedirectResponse:
    """Delete session from Redis and clear cookie."""
    settings = get_settings()
    redis = get_client()

    if admin_session and redis is not None:
        sid = session_auth.verify_session(
            admin_session, settings.admin_token.get_secret_value()
        )
        if sid:
            await session_auth.delete_session(redis, sid)

    resp = RedirectResponse(url="/admin/ui/login", status_code=302)
    resp.delete_cookie(key=_COOKIE_NAME, httponly=True, samesite="strict")
    logger.info("admin_ui.logout")
    return resp


@router.get("/", response_class=HTMLResponse)
async def get_dashboard(
    request: Request,
    _sid: Annotated[str, Depends(require_session)],
) -> HTMLResponse:
    """Dashboard page: 4 KPI cards + 24h sparklines."""
    try:
        kpis = await fetch_kpis()
        sparklines = await fetch_sparklines()
    except Exception:
        logger.warning("admin_ui.dashboard.metrics_fetch_failed", exc_info=True)
        kpis = KPISnapshot()
        sparklines = SparklineData()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"kpis": kpis, "sparklines": sparklines},
    )


@router.get("/partials/kpis", response_class=HTMLResponse)
async def get_kpis_partial(
    request: Request,
    _sid: Annotated[str, Depends(require_session)],
) -> Response:
    """HTMX partial — KPI cards fragment for polling updates (every 5s)."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/admin/ui/", status_code=302)

    try:
        kpis = await fetch_kpis()
    except Exception:
        logger.warning("admin_ui.kpis_partial.fetch_failed", exc_info=True)
        kpis = KPISnapshot()

    return templates.TemplateResponse(
        request,
        "partials/kpis.html",
        {"kpis": kpis},
    )


@router.get("/jobs/{job_id}/stderr_proxy", response_class=HTMLResponse)
async def get_stderr_proxy(
    job_id: str,
    request: Request,
    _sid: Annotated[str, Depends(require_session)],
) -> HTMLResponse:
    """Session-cookie-authenticated proxy for /admin/codex/jobs/{id}/stderr.

    Browser JS in the job detail modal calls this URL (no X-Admin-Token needed
    from the client side). This handler server-side fetches the real stderr
    endpoint using the admin token from settings and returns plain text.
    """
    import httpx as _httpx  # noqa: PLC0415

    settings = get_settings()
    admin_token = settings.admin_token.get_secret_value()
    target = f"http://localhost:8000/admin/codex/jobs/{job_id}/stderr"

    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.get(
                target,
                headers={"X-Admin-Token": admin_token},
                timeout=10.0,
            )
        return HTMLResponse(content=resp.text, status_code=resp.status_code)
    except Exception:
        logger.warning("admin_ui.stderr_proxy.failed", job_id=job_id, exc_info=True)
        return HTMLResponse(content="Error fetching stderr", status_code=502)


# ── Sub-routers — session guard applied via dependencies ──────────────────────
# Passing require_session as a router-level dependency means every route in
# these sub-routers inherits the cookie auth check without each handler having
# to declare it explicitly.

from src.admin_ui.audit_page_routes import router as _audit_router  # noqa: E402
from src.admin_ui.jobs_page_routes import router as _jobs_router  # noqa: E402
from src.admin_ui.keys_page_routes import router as _keys_router  # noqa: E402
from src.admin_ui.tiers_page_routes import router as _tiers_router  # noqa: E402
from src.admin_ui.users_page_routes import router as _users_router  # noqa: E402

router.include_router(_keys_router, dependencies=[Depends(require_session)])
router.include_router(_tiers_router, dependencies=[Depends(require_session)])
router.include_router(_jobs_router, dependencies=[Depends(require_session)])
router.include_router(_audit_router, dependencies=[Depends(require_session)])
router.include_router(_users_router, dependencies=[Depends(require_session)])
