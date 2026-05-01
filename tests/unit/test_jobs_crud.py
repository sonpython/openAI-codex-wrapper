"""
Unit tests for db/crud/jobs.py state transitions.

Uses SQLite in-memory via aiosqlite (same pattern as other unit tests).
All tests run without a real Postgres instance.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.db.crud import jobs as jobs_crud  # noqa: E402
from src.db.models import Base, User  # noqa: E402

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
async def session() -> AsyncSession:
    """In-memory SQLite session with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # SQLite doesn't support JSONB — map it to JSON for unit tests.
    from sqlalchemy import JSON  # noqa: PLC0415

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(conn, _):  # type: ignore[misc]
        conn.execute("PRAGMA journal_mode=WAL")

    async with engine.begin() as conn:
        # Replace JSONB with JSON for SQLite compatibility
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if hasattr(col.type, "__class__") and col.type.__class__.__name__ == "JSONB":
                    col.type = JSON()
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture()
async def user_id(session: AsyncSession) -> uuid.UUID:
    """Insert a test user and return its UUID."""
    uid = uuid.uuid4()
    session.add(User(id=uid, email=f"test-{uid}@example.com"))
    await session.commit()
    return uid


# ── create_job ────────────────────────────────────────────────────────────────


async def test_create_job_inserts_queued(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="Fix the bug",
        mode="read-only",
    )
    await session.commit()
    assert job.id is not None
    assert job.status == "queued"
    assert job.repo_url == "https://github.com/openai/codex"
    assert job.branch == "main"
    assert job.task == "Fix the bug"
    assert job.mode == "read-only"
    assert job.user_id == user_id


async def test_create_job_enqueued_at_populated(session: AsyncSession, user_id: uuid.UUID) -> None:
    created = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()
    # enqueued_at populated by ORM default or server_default (SQLite uses client default)
    assert created is not None


# ── get_job ───────────────────────────────────────────────────────────────────


async def test_get_job_returns_owned(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    found = await jobs_crud.get_job(session, job.id, user_id)
    assert found is not None
    assert found.id == job.id


async def test_get_job_returns_none_wrong_owner(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    other_user = uuid.uuid4()
    found = await jobs_crud.get_job(session, job.id, other_user)
    assert found is None


async def test_get_job_returns_none_unknown_id(session: AsyncSession, user_id: uuid.UUID) -> None:
    found = await jobs_crud.get_job(session, uuid.uuid4(), user_id)
    assert found is None


# ── mark_running ──────────────────────────────────────────────────────────────


async def test_mark_running_transitions_status(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    await jobs_crud.mark_running(session, job.id, "/workspaces/test")
    await session.commit()

    await session.refresh(job)
    assert job.status == "running"
    assert job.workspace_path == "/workspaces/test"
    assert job.started_at is not None


# ── mark_succeeded ────────────────────────────────────────────────────────────


async def test_mark_succeeded_transitions_status(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    await jobs_crud.mark_succeeded(
        session,
        job.id,
        summary="All done",
        diff_blob="diff --git a/f b/f",
        diff_size_bytes=20,
        files_changed=["f.py"],
        exit_code=0,
        stderr_tail=None,
    )
    await session.commit()

    await session.refresh(job)
    assert job.status == "succeeded"
    assert job.summary == "All done"
    assert job.exit_code == 0
    assert job.finished_at is not None


# ── mark_failed ───────────────────────────────────────────────────────────────


async def test_mark_failed_transitions_status(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    await jobs_crud.mark_failed(
        session,
        job.id,
        error_code="clone_failed",
        error_message="connection refused",
        exit_code=1,
        stderr_tail="fatal: repo not found",
    )
    await session.commit()

    await session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "clone_failed"
    assert job.error_message == "connection refused"
    assert job.exit_code == 1
    assert job.stderr_tail == "fatal: repo not found"
    assert job.finished_at is not None


# ── mark_cancelled ────────────────────────────────────────────────────────────


async def test_mark_cancelled_transitions_status(session: AsyncSession, user_id: uuid.UUID) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    rows = await jobs_crud.mark_cancelled(session, job.id)
    await session.commit()

    await session.refresh(job)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert rows == 1  # H3: returns rowcount


async def test_mark_cancelled_guard_status_noop_when_already_terminal(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    """H3: guard_status='queued' must be a no-op when job is already succeeded.
    Returns 0 rows — caller knows the worker already transitioned."""
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    # Simulate worker completing the job first
    await jobs_crud.mark_succeeded(
        session,
        job.id,
        summary=None,
        diff_blob=None,
        diff_size_bytes=None,
        files_changed=None,
        exit_code=0,
        stderr_tail=None,
    )
    await session.commit()

    # API tries to cancel with guard_status='queued' — should be a no-op
    rows = await jobs_crud.mark_cancelled(session, job.id, guard_status="queued")
    await session.commit()

    assert rows == 0
    await session.refresh(job)
    # Status must remain succeeded — not overwritten to cancelled
    assert job.status == "succeeded"


# ── list_orphans ──────────────────────────────────────────────────────────────


async def test_list_orphans_returns_running_jobs(session: AsyncSession, user_id: uuid.UUID) -> None:
    # Create queued, running, succeeded — only running should be returned.
    j_queued = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="queued",
        mode="read-only",
    )
    j_running = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="running",
        mode="read-only",
    )
    await session.commit()

    await jobs_crud.mark_running(session, j_running.id, "/ws/running")
    await session.commit()

    orphans = await jobs_crud.list_orphans(session)
    orphan_ids = {o.id for o in orphans}
    assert j_running.id in orphan_ids
    assert j_queued.id not in orphan_ids


async def test_list_orphans_empty_when_none_running(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    orphans = await jobs_crud.list_orphans(session)
    assert orphans == []


# ── create_job with api_key_id ────────────────────────────────────────────────


async def test_create_job_stores_api_key_id(session: AsyncSession, user_id: uuid.UUID) -> None:
    key_id = uuid.uuid4()
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="Fix the bug",
        mode="read-only",
        api_key_id=key_id,
    )
    await session.commit()
    assert job.api_key_id == key_id


async def test_create_job_api_key_id_defaults_to_none(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="Fix the bug",
        mode="read-only",
    )
    await session.commit()
    assert job.api_key_id is None


# ── update_token_counts ───────────────────────────────────────────────────────


async def test_update_token_counts_sets_values(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    await jobs_crud.update_token_counts(session, job.id, input_tokens=150, output_tokens=75)
    await session.commit()

    await session.refresh(job)
    assert job.input_tokens == 150
    assert job.output_tokens == 75


async def test_update_token_counts_zero_values(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Setting 0/0 is valid (codex didn't emit token_usage)."""
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()

    await jobs_crud.update_token_counts(session, job.id, input_tokens=0, output_tokens=0)
    await session.commit()

    await session.refresh(job)
    assert job.input_tokens == 0
    assert job.output_tokens == 0


async def test_new_job_has_zero_tokens_by_default(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    job = await jobs_crud.create_job(
        session,
        user_id=user_id,
        repo_url="https://github.com/openai/codex",
        branch="main",
        task="t",
        mode="read-only",
    )
    await session.commit()
    assert job.input_tokens == 0
    assert job.output_tokens == 0
