"""
FastAPI application factory.

``create_app()`` is the entry point used by uvicorn:

    uv run uvicorn src.gateway.app:create_app --factory ...

Lifespan init order (mirrors Architecture section of phase-00 spec):
  1. Load Settings (pydantic-settings raises immediately on missing required env)
  2. Configure structlog
  3. Init OTEL tracer (no-op when OTEL_EXPORTER_OTLP_ENDPOINT unset)
  4. Open SQLAlchemy engines + connection check
  5. Open Redis pool + PING
  6. Routers mounted (health, metrics; placeholders for later phases)
  7. Yield → app ready for traffic

Shutdown (reverse order):
  1. Close Redis pool
  2. Dispose DB engines
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.codex import auth_session
from src.db.engine import close_engines, get_bg_engine, get_main_engine, init_engines
from src.gateway.health import router as health_router
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.routes.admin_api_keys import router as admin_api_keys_router
from src.gateway.routes.chat_completions import router as chat_completions_router
from src.gateway.routes.models import router as models_router
from src.observability.logging import configure_logging
from src.observability.metrics import make_metrics_app
from src.observability.tracing import configure_tracing
from src.redis_client import close_redis, get_client, init_redis
from src.settings import get_settings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of all shared resources."""
    settings = get_settings()

    # 1. Structured logging — must come first so all subsequent log calls work.
    configure_logging(settings)
    logger.info("gateway_starting", env=settings.wrapper_env)

    # 2. OTEL — no-op when endpoint not configured.
    configure_tracing(settings, app)

    # 3. Database engines (main + background pools) + connectivity check.
    # Exceptions propagate — uvicorn exits non-zero on misconfigured DATABASE_URL.
    init_engines(settings)
    async with get_main_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    async with get_bg_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info(
        "db_pool_opened",
        main_pool_size=settings.db_pool_size,
        bg_pool_size=settings.bg_db_pool_size,
    )

    # 4. Redis pool + PING check.
    # Exceptions propagate — uvicorn exits non-zero on misconfigured REDIS_URL.
    init_redis(settings)
    await get_client().ping()  # type: ignore[union-attr]
    logger.info("redis_pool_opened")

    # 5. Codex auth-session background poller.
    # Default-deny: codex_session_healthy=False until first probe succeeds.
    # Task is cancelled + awaited on shutdown to prevent dangling coroutines.
    poll_task: asyncio.Task[None] = await auth_session.start_poller(app)
    logger.info("codex_session_poller_started")

    logger.info("gateway_ready")
    yield

    # ── Shutdown ───────────────────────────────────────────────────────────
    logger.info("gateway_shutting_down")
    poll_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poll_task
    await close_redis()
    await close_engines()
    logger.info("gateway_shutdown_complete")


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application.

    Routers for v1/chat, v1/responses, v1/codex are added in phases 03-05.
    This phase mounts only health + metrics endpoints so the container starts
    and passes readiness probes.
    """
    settings = get_settings()

    app = FastAPI(
        title="codex-wrapper",
        version="0.1.0",
        description="OpenAI-compatible API gateway backed by the Codex CLI",
        docs_url="/docs" if settings.wrapper_env != "prod" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ── Exception handlers ─────────────────────────────────────────────────
    # Reshape FastAPI's default 422 ValidationError into OpenAI's 400 envelope
    # so SDK clients parse it as APIStatusError with type="invalid_request_error".
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        msg = first.get("msg", "Request validation error")
        loc = first.get("loc", ())
        param = ".".join(str(p) for p in loc if p != "body") or None
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": str(msg),
                    "type": "invalid_request_error",
                    "param": param,
                    "code": "invalid_request_error",
                }
            },
        )

    # ── Middleware (order: outermost = last registered with add_middleware) ──
    # AuthMiddleware is a raw ASGI middleware — avoids Starlette SSE buffering.
    # It must run AFTER logging/tracing middlewares (those are added by phases
    # 07/tracing) and BEFORE routers. Since add_middleware wraps in LIFO order,
    # register AuthMiddleware last so it executes closest to the route handlers.
    app.add_middleware(AuthMiddleware)

    # ── Routers ────────────────────────────────────────────────────────────
    # Health / readiness probes (no auth required — in AuthMiddleware skip-list)
    app.include_router(health_router)

    # OpenAI-compatible model listing (auth required via middleware)
    app.include_router(models_router)

    # Admin key management (auth via X-Admin-Token dependency, not bearer)
    app.include_router(admin_api_keys_router, prefix="/admin")

    # Chat completions — phase 03 (auth enforced by AuthMiddleware)
    app.include_router(chat_completions_router)

    # Prometheus metrics scrape endpoint — make_metrics_app() returns Any (untyped lib)
    app.mount("/metrics", make_metrics_app())

    return app
