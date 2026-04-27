"""
Arq worker entry point.

Run with:
    uv run arq src.workers.arq_worker.WorkerSettings

WorkerSettings wires:
  - Redis connection from settings.arq_redis_url (falls back to redis_url).
  - functions=[run_codex_job] — the only registered task.
  - on_startup: orphan recovery + inject db session into ctx.
  - on_shutdown: close db session.
  - max_jobs: from settings.arq_max_jobs (default 4).
  - job_timeout: settings.job_timeout_seconds + 60 (Arq hard kill > our soft).

ctx["redis"] is injected by Arq automatically.
ctx["db"] is the shared bg_session injected in on_startup.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron

from src.db.engine import bg_session, close_engines, init_engines
from src.observability.logging import configure_logging
from src.settings import get_settings
from src.workers.janitor import cleanup_stale_workspaces, purge_old_audit_logs
from src.workers.job_handlers import recover_orphan_jobs, run_codex_job

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Worker startup hook: configure logging, init DB, recover orphans."""
    settings = get_settings()
    configure_logging(settings)
    init_engines(settings)
    logger.info("arq_worker.starting", max_jobs=settings.arq_max_jobs)

    # Recover any jobs left in 'running' state from a previous worker crash.
    redis = ctx["redis"]
    async with bg_session() as session:
        await recover_orphan_jobs(session, redis)

    logger.info("arq_worker.ready")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Worker shutdown hook: dispose DB engines."""
    logger.info("arq_worker.shutting_down")
    await close_engines()
    logger.info("arq_worker.shutdown_complete")


def _get_redis_settings() -> RedisSettings:
    settings = get_settings()
    url = settings.arq_redis_url or settings.redis_url
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    """Arq WorkerSettings — discovered by ``arq src.workers.arq_worker.WorkerSettings``."""

    functions = [run_codex_job]
    on_startup = startup
    on_shutdown = shutdown
    keep_result_seconds = 86_400  # 24h — matches Redis replay list TTL

    # Cron tasks — registered at class level; _build() may override if needed.
    # cleanup_stale_workspaces: every 10 minutes
    # purge_old_audit_logs: daily at 03:00 UTC
    cron_jobs = [
        cron(cleanup_stale_workspaces, minute={0, 10, 20, 30, 40, 50}),
        cron(purge_old_audit_logs, hour=3, minute=0),
    ]

    @classmethod
    def _build(cls) -> None:
        """Populate dynamic settings from environment at import time."""
        settings = get_settings()
        cls.redis_settings = _get_redis_settings()  # type: ignore[attr-defined]
        cls.max_jobs = settings.arq_max_jobs  # type: ignore[attr-defined]
        # Arq hard-kills the coroutine after job_timeout; set slightly above
        # our soft timeout so our except TimeoutError branch runs first.
        cls.job_timeout = settings.job_timeout_seconds + 60  # type: ignore[attr-defined]


# Build dynamic fields immediately on import so Arq picks them up.
WorkerSettings._build()
