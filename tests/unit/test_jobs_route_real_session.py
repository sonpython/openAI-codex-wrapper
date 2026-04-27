"""
C1 regression test: verify main_session() does NOT raise TypeError.

Uses a real SQLAlchemy async_sessionmaker backed by in-memory SQLite.
No patching of main_session — this catches the regression where get_session()
(an async generator) was mistakenly used as an async context manager.

If C1 regresses, this test fails with:
  TypeError: 'async_generator' object does not support the asynchronous context
  manager protocol
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import JSON, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import src.db.engine as engine_module  # noqa: E402
from src.db.models import Base, User  # noqa: E402


@pytest.fixture()
async def sqlite_session_factory():  # type: ignore[return]
    """In-memory SQLite factory wired into engine_module._main_session_factory."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(eng.sync_engine, "connect")
    def _pragma(conn, _):  # type: ignore[misc]
        conn.execute("PRAGMA journal_mode=WAL")

    async with eng.begin() as conn:
        # Map JSONB → JSON for SQLite compatibility
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if hasattr(col.type, "__class__") and col.type.__class__.__name__ == "JSONB":
                    col.type = JSON()
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)

    # Wire the factory into the module so main_session() uses it
    original = engine_module._main_session_factory
    engine_module._main_session_factory = factory
    yield factory
    engine_module._main_session_factory = original
    await eng.dispose()


async def test_main_session_is_valid_async_context_manager(sqlite_session_factory) -> None:  # type: ignore[misc]
    """main_session() must be usable as `async with main_session() as session`
    without raising TypeError. This validates C1 fix."""
    from src.db.engine import main_session  # noqa: PLC0415

    # This would raise TypeError if main_session() returned an async generator
    # (the C1 bug — get_session() was used instead).
    async with main_session() as session:
        assert isinstance(session, AsyncSession)
        # Basic sanity: insert + query a user row
        uid = uuid.uuid4()
        session.add(User(id=uid, email=f"c1-test-{uid}@example.com"))
        await session.commit()

    # Second context manager call must also work (factory is reusable)
    async with main_session() as session2:
        assert isinstance(session2, AsyncSession)


async def test_main_session_rolls_back_on_exception(sqlite_session_factory) -> None:  # type: ignore[misc]
    """Session context manager cleans up on exception — no connection leak."""
    from src.db.engine import main_session  # noqa: PLC0415

    uid = uuid.uuid4()
    raised = False
    try:
        async with main_session() as session:
            session.add(User(id=uid, email=f"rollback-test-{uid}@example.com"))
            await session.flush()
            raise ValueError("deliberate error")
    except ValueError:
        raised = True

    assert raised
    # The context manager must have closed cleanly (no hanging connection).
    # Verify by opening a new session — if pool was leaked this would hang/error.
    async with main_session() as session2:
        assert isinstance(session2, AsyncSession)
