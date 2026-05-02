"""
Authentication middleware — raw ASGI implementation.

Raw ASGI is used instead of BaseHTTPMiddleware to avoid Starlette's response
buffering, which breaks Server-Sent Events streaming (phases 03, 04). With
BaseHTTPMiddleware, the entire response body is buffered before being sent to
the client, making SSE non-functional.

Auth flow:
  1. Paths in AUTH_SKIP_PATHS are passed through without any token check.
     /admin/* paths have their own X-Admin-Token check in the router.
  2. Extract "Authorization: Bearer cwk_..." header.
  3. Prefix-indexed DB lookup + argon2id verify (main pool).
  4. On success: stash api_key row attrs in request.state; fire-and-forget
     last_used_at update via bg pool.
  5. On failure: return 401 with OpenAI-shaped JSON immediately.

Errors inside the auth check itself (DB unreachable, etc.) return 500 with
the standard OpenAI error envelope — no internal details leaked.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy.exc
import structlog
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.auth.bearer import extract_bearer
from src.auth.errors import (
    internal_error_response,
    invalid_api_key_response,
    service_unavailable_response,
)
from src.db.crud.api_keys import get_active_by_prefix_and_verify, update_last_used_fire_and_forget
from src.db.engine import main_session
from src.observability.metrics import AUTH_REJECTIONS

logger = structlog.get_logger(__name__)

# Exact paths that bypass bearer-token auth (O(1) frozenset lookup).
# /healthz, /readyz, /metrics: no legitimate sub-paths — exact match only.
# /admin/* has its own X-Admin-Token dependency at the route layer; listed here
# so the bearer middleware doesn't reject admin requests with no Bearer header.
# Adding any route under /v1/* automatically requires auth (default-deny).
AUTH_SKIP_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/metrics",
        "/docs",  # FastAPI swagger UI (exact)
        "/openapi.json",  # OpenAPI schema (exact)
        "/redoc",  # ReDoc UI (exact)
    }
)

# Prefix matches for paths that have sub-routes (e.g. /docs/oauth2-redirect).
# Kept minimal: only paths where sub-routes legitimately exist and require bypass.
# IMPORTANT: /healthz, /readyz, /metrics are NOT here — they must be exact only.
# /_internal/metrics is mounted as an ASGI sub-app so the actual path is
# /_internal/metrics/ — match by prefix to allow both variants for Prometheus scrape.
AUTH_SKIP_PREFIXES: tuple[str, ...] = ("/admin/", "/docs/", "/_internal/metrics")


def _should_skip(path: str) -> bool:
    if path in AUTH_SKIP_PATHS:
        return True
    return any(path.startswith(p) for p in AUTH_SKIP_PREFIXES)


class AuthMiddleware:
    """Raw ASGI authentication middleware.

    Validates Bearer tokens on every request not in the skip-list.
    Stores resolved identity in request.state for downstream use.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        if _should_skip(path):
            await self.app(scope, receive, send)
            return

        # Build a minimal Request to access headers conveniently.
        request = Request(scope, receive)
        headers = Headers(scope=scope)

        plaintext = extract_bearer(headers)
        if plaintext is None:
            AUTH_REJECTIONS.labels(reason="missing_bearer").inc()
            response = invalid_api_key_response()
            await response(scope, receive, send)
            return

        try:
            api_key = await self._authenticate(plaintext)
        except (TimeoutError, sqlalchemy.exc.TimeoutError):
            # Pool exhausted — safe to surface as 503 so callers can retry.
            # Do NOT return 401 here: that would leak info about key validity.
            logger.warning("auth.db_pool_timeout")
            response = service_unavailable_response()
            await response(scope, receive, send)
            return
        except sqlalchemy.exc.SQLAlchemyError:
            logger.exception("auth.db_unavailable")
            response = service_unavailable_response()
            await response(scope, receive, send)
            return
        except Exception:
            logger.exception("auth.middleware.unexpected_error")
            response = internal_error_response()
            await response(scope, receive, send)
            return

        if api_key is None:
            AUTH_REJECTIONS.labels(reason="invalid_key").inc()
            response = invalid_api_key_response()
            await response(scope, receive, send)
            return

        # Stash resolved identity — downstream handlers read from request.state.
        request.state.api_key_id = api_key.id
        request.state.user_id = api_key.user_id
        request.state.tier = api_key.tier

        # Best-effort timestamp update — never blocks request path (C8).
        update_last_used_fire_and_forget(api_key.id)

        await self.app(scope, receive, send)

    @staticmethod
    async def _authenticate(plaintext: str) -> Any:  # returns ApiKey | None
        """Open a main-pool session and verify the token against the DB.

        Uses main_session() context manager directly — not the FastAPI
        get_session() generator dependency — so the session is closed
        deterministically on block exit, preventing pool exhaustion under load.
        """
        async with main_session() as session:
            return await get_active_by_prefix_and_verify(session, plaintext)
