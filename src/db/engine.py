"""
SQLAlchemy async engine and session factories.

Two pools are created (addresses Red Team C8 + C9):

  main engine  — pool_size=20, max_overflow=10, pool_timeout=2.0
    Capacity math: 100 RPS × ~50ms p99 (argon2 + auth lookup) ≈ 5 simultaneous
    connections under normal load; double that under burst ≈ 10-15.  max_overflow
    lets us burst to 30 total.  pool_timeout=2.0 means callers see a fast 503
    under extreme overload instead of hanging for SQLAlchemy's default 30 s.

  background engine — pool_size=3, max_overflow=0, pool_timeout=0.5
    Used for fire-and-forget writes (audit log, last_used_at updates).
    On acquire timeout the CALLER must log WARN and drop the write — background
    tasks MUST NOT block the request path.  pool_overflow=0 keeps it hard-capped.

Deployment model: 1 uvicorn worker per container; scale horizontally via Docker
Compose / container replicas.  The async event loop handles concurrent requests
within that single worker without additional OS threads.

Connection budget per gateway replica:
  1 worker × (20+10 main + 3 bg) = 33 connections/replica.

With N replicas: Postgres max_connections must be ≥ N×33 + DB-internal headroom
(~10) + alembic one-shot (~2).  Phase 10 sets POSTGRES_MAX_CONNECTIONS
accordingly, or adds pgBouncer if N×33 exceeds a comfortable threshold.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.settings import Settings

# Module-level engine references — initialised lazily via init_engines().
_main_engine: Any = None
_bg_engine: Any = None

_main_session_factory: async_sessionmaker[AsyncSession] | None = None
_bg_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_main_engine() -> Any:
    """Return the main async engine, or None if not yet initialised."""
    return _main_engine


def get_bg_engine() -> Any:
    """Return the background async engine, or None if not yet initialised."""
    return _bg_engine


def init_engines(settings: Settings) -> None:
    """Create both engine instances.  Called once from the app lifespan."""
    global _main_engine, _bg_engine, _main_session_factory, _bg_session_factory

    _main_engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        echo=False,
    )
    _bg_engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.bg_db_pool_size,
        max_overflow=0,  # hard cap — drop writes on contention
        pool_timeout=settings.bg_db_pool_timeout,
        echo=False,
    )
    _main_session_factory = async_sessionmaker(
        _main_engine, expire_on_commit=False, class_=AsyncSession
    )
    _bg_session_factory = async_sessionmaker(
        _bg_engine, expire_on_commit=False, class_=AsyncSession
    )


async def close_engines() -> None:
    """Dispose both engines.  Called from lifespan shutdown."""
    if _main_engine is not None:
        await _main_engine.dispose()
    if _bg_engine is not None:
        await _bg_engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a main-pool session, auto-closed on exit."""
    assert _main_session_factory is not None, "DB not initialised"
    async with _main_session_factory() as session:
        yield session


def main_session() -> AsyncSession:
    """Return a main-pool session context manager (caller owns lifecycle).

    Usage:
        async with main_session() as s:
            result = await s.execute(...)
    Session is closed deterministically on block exit.
    Use this in non-FastAPI contexts (e.g. raw ASGI middleware) where the
    get_session() generator dependency cannot be injected.
    """
    assert _main_session_factory is not None, "DB not initialised"
    return _main_session_factory()


def bg_session() -> AsyncSession:
    """Return a background-writes session (caller owns lifecycle).

    Usage:
        async with bg_session() as s:
            s.add(...)
            await s.commit()
    On pool timeout a ``TimeoutError`` is raised — caller MUST catch and log WARN.
    """
    assert _bg_session_factory is not None, "DB not initialised"
    return _bg_session_factory()
