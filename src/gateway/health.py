"""
Health and readiness check routes.

  GET /healthz — liveness: always 200 {"status": "ok"}.
  GET /readyz  — readiness: 200 only when Postgres + Redis are reachable;
                 503 with error detail if either is down.

These endpoints are intentionally kept dependency-free at the module level so
they are importable before the DB/Redis pools are initialised (helps with
test isolation).

IMPORTANT: Use accessor functions (get_main_engine, get_client) instead of
module-level imports of the private singletons. Direct `from … import _name`
binds None at import time and never sees the later assignment made by
init_engines() / init_redis(). Accessor functions call through to the live
module-level variable on every invocation, so they always reflect the current
state.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.db.engine import get_main_engine
from src.redis_client import get_client

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness probe — always returns 200."""
    return JSONResponse({"status": "ok"})


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness probe — checks Postgres, Redis, and Codex session health.

    Returns 200 when all three are healthy; 503 with an ``errors`` list
    otherwise. Caddy upstream health-checks and Kubernetes readiness probes
    consume this.
    """
    errors: list[str] = []

    # ── Postgres check ────────────────────────────────────────────────────
    engine = get_main_engine()
    if engine is None:
        errors.append("db: engine not initialised")
    else:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("readyz_db_fail", error=str(exc))
            errors.append("db: unreachable")

    # ── Redis check ───────────────────────────────────────────────────────
    client = get_client()
    if client is None:
        errors.append("redis: client not initialised")
    else:
        try:
            await client.ping()
        except Exception as exc:
            logger.warning("readyz_redis_fail", error=str(exc))
            errors.append("redis: unreachable")

    # ── Codex session check ───────────────────────────────────────────────
    # Reads app.state set by auth_session background poller (default-deny).
    # H-3 fix: default False (fail-closed). If lifespan never ran (e.g. bare
    # test app or crashed startup), the attribute is absent → report unhealthy.
    # Tests that need a healthy codex state must set app.state.codex_session_healthy
    # explicitly via fixture. This matches spec §7: "default-deny on startup".
    session_healthy: bool = getattr(request.app.state, "codex_session_healthy", False)
    if not session_healthy:
        errors.append("codex: session unhealthy")

    if errors:
        return JSONResponse({"status": "unavailable", "errors": errors}, status_code=503)

    return JSONResponse({"status": "ok"})
