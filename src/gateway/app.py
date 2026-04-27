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
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from src.codex import auth_session
from src.db.engine import close_engines, get_bg_engine, get_main_engine, init_engines
from src.gateway.health import router as health_router
from src.gateway.middleware.auth import AuthMiddleware
from src.gateway.middleware.edge_ip_limiter import EdgeIPLimiter
from src.gateway.middleware.observability import ObservabilityMiddleware
from src.gateway.middleware.rate_limit import RateLimitMiddleware
from src.gateway.middleware.request_id import RequestIDMiddleware
from src.gateway.middleware.timeout import TimeoutMiddleware
from src.gateway.middleware.usage_tracking import UsageTrackingMiddleware
from src.gateway.routes.admin_api_keys import router as admin_api_keys_router
from src.gateway.routes.admin_codex_stderr import router as admin_stderr_router
from src.gateway.routes.chat_completions import router as chat_completions_router
from src.gateway.routes.jobs import router as jobs_router
from src.gateway.routes.models import router as models_router
from src.gateway.routes.responses import router as responses_router
from src.observability.logging import configure_logging
from src.observability.metrics import make_metrics_app
from src.observability.tracing import configure_tracing
from src.redis_client import close_redis, get_client, init_redis
from src.settings import get_settings

# Module-level Arq pool reference — accessed by routes/jobs.py via _arq_pool.
# Initialised in lifespan; None before startup and after shutdown.
_arq_pool: Any = None

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

    # 5. Arq connection pool for job enqueue (gateway enqueues; worker executes).
    global _arq_pool
    try:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        arq_redis_url = settings.arq_redis_url or settings.redis_url
        _arq_pool = await create_pool(RedisSettings.from_dsn(arq_redis_url))
        logger.info("arq_pool_opened", redis_url=arq_redis_url)
    except Exception:
        # Non-fatal: jobs route will return 503 when pool is None.
        logger.warning("arq_pool.init_failed", exc_info=True)
        _arq_pool = None

    # 6. Codex auth-session background poller.
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
    if _arq_pool is not None:
        try:
            await _arq_pool.aclose()
        except Exception:
            logger.warning("arq_pool.close_failed", exc_info=True)
        _arq_pool = None
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
        raw_msg = str(first.get("msg", "Request validation error"))
        loc = first.get("loc", ())
        param: str | None = ".".join(str(p) for p in loc if p != "body") or None

        # Parse structured prefix emitted by ResponsesRequest.reject_unsupported_fields:
        # "unsupported_parameter:<field>:<reason>"
        code = "invalid_request_error"
        msg = raw_msg
        prefix = "unsupported_parameter:"
        if prefix in raw_msg:
            # msg may be wrapped by pydantic: "Value error, unsupported_parameter:..."
            tail = raw_msg.split(prefix, 1)[1]
            parts = tail.split(":", 1)
            param = parts[0].strip()
            code = "unsupported_parameter"
            msg = parts[1].strip() if len(parts) > 1 else tail.strip()

        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": msg,
                    "type": "invalid_request_error",
                    "param": param,
                    "code": code,
                }
            },
        )

    # ── Middleware (order: outermost = last registered with add_middleware) ──
    #
    # FastAPI/Starlette add_middleware uses LIFO wrapping:
    #   last add_middleware call → outermost wrapper → first to see REQUEST
    #
    # Desired REQUEST flow (outer → inner):
    #   RequestID → Timeout → Observability → EdgeIPLimiter → Auth → RateLimit → UsageTracking → route
    #
    # TimeoutMiddleware sits between RequestID and Observability so:
    #   - It has a request_id in scope for structured logs
    #   - 504 timeout responses are tracked by ObservabilityMiddleware metrics
    #
    # All rate-limit middlewares resolve the Redis client lazily via get_client()
    # on each request, so they can be registered here (before lifespan runs).
    #
    # Registration order (first registered = innermost = closest to route):

    # 1. UsageTrackingMiddleware — innermost; observes response status + body.
    app.add_middleware(UsageTrackingMiddleware)

    # 2. RateLimitMiddleware — enforces RPM/TPM/concurrent; stashes RL headers.
    app.add_middleware(RateLimitMiddleware)

    # 3. AuthMiddleware — populates request.state.api_key_id / user_id / tier.
    app.add_middleware(AuthMiddleware)

    # 4. EdgeIPLimiter — pre-auth IP bucket before any argon2 work.
    app.add_middleware(EdgeIPLimiter)

    # 5. ObservabilityMiddleware — times requests, emits Prometheus + log.
    #    Runs after RequestID so request_id is available in scope state.
    app.add_middleware(ObservabilityMiddleware)

    # 6. TimeoutMiddleware — per-route hard timeouts; 504 on exceed.
    #    Placed between Observability and RequestID so timeout 504s are
    #    tracked by metrics and carry a request_id in logs.
    app.add_middleware(TimeoutMiddleware)

    # 7. RequestIDMiddleware — outermost; assigns request_id first so all
    #    downstream middleware and handlers have it in structlog context.
    app.add_middleware(RequestIDMiddleware)

    # ── Routers ────────────────────────────────────────────────────────────
    # Health / readiness probes (no auth required — in AuthMiddleware skip-list)
    app.include_router(health_router)

    # OpenAI-compatible model listing (auth required via middleware)
    app.include_router(models_router)

    # Admin key management (auth via X-Admin-Token dependency, not bearer)
    app.include_router(admin_api_keys_router, prefix="/admin")

    # Admin stderr retrieval (auth via X-Admin-Token, no prefix — path is /admin/codex/...)
    app.include_router(admin_stderr_router)

    # Chat completions — phase 03 (auth enforced by AuthMiddleware)
    app.include_router(chat_completions_router)

    # Responses API — phase 04 (auth enforced by AuthMiddleware)
    app.include_router(responses_router)

    # Jobs API — phase 05 (auth enforced by AuthMiddleware)
    app.include_router(jobs_router)

    # Prometheus metrics scrape endpoint — internal only; Caddy MUST NOT proxy this path.
    # make_metrics_app() returns Any (untyped prometheus_client library).
    app.mount(settings.internal_metrics_path, make_metrics_app())

    return app
