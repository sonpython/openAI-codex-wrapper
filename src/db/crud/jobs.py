"""
CRUD helpers for the jobs table.

All state-transition helpers use explicit UPDATE ... RETURNING to avoid
loading stale ORM instances. Callers pass an open AsyncSession and are
responsible for commit/rollback.

Status lifecycle:
  queued → running → succeeded | failed | cancelled

``list_orphans`` returns jobs in ``running`` state — used by the worker
startup hook to recover from a crash mid-execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Job


def _now() -> datetime:
    return datetime.now(UTC)


async def create_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    repo_url: str,
    branch: str,
    task: str,
    mode: str,
) -> Job:
    """Insert a new job row with status=queued and return it."""
    job = Job(
        user_id=user_id,
        status="queued",
        repo_url=repo_url,
        branch=branch,
        task=task,
        mode=mode,
    )
    session.add(job)
    await session.flush()  # populate id + server_default enqueued_at
    return job


async def get_job(
    session: AsyncSession,
    job_id: UUID,
    user_id: UUID,
) -> Job | None:
    """Return job by id scoped to owner, or None if not found / wrong owner."""
    result = await session.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    return result.scalar_one_or_none()


async def get_job_unscoped(session: AsyncSession, job_id: UUID) -> Job | None:
    """Return job by id without ownership check (used internally by worker)."""
    result = await session.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


async def mark_running(
    session: AsyncSession,
    job_id: UUID,
    workspace_path: str,
) -> None:
    """Transition job to running state, recording started_at and workspace."""
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="running",
            started_at=_now(),
            workspace_path=workspace_path,
        )
    )


async def mark_succeeded(
    session: AsyncSession,
    job_id: UUID,
    *,
    summary: str | None,
    diff_blob: str | None,
    diff_size_bytes: int | None,
    files_changed: list[str] | None,
    exit_code: int | None,
    stderr_tail: str | None,
) -> None:
    """Transition job to succeeded with result fields."""
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="succeeded",
            finished_at=_now(),
            summary=summary,
            diff_blob=diff_blob,
            diff_size_bytes=diff_size_bytes,
            files_changed=files_changed,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
        )
    )


async def mark_failed(
    session: AsyncSession,
    job_id: UUID,
    *,
    error_code: str,
    error_message: str,
    exit_code: int | None = None,
    stderr_tail: str | None = None,
) -> None:
    """Transition job to failed state with diagnostic fields."""
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="failed",
            finished_at=_now(),
            error_code=error_code,
            error_message=error_message,
            exit_code=exit_code,
            stderr_tail=stderr_tail,
        )
    )


async def mark_cancelled(
    session: AsyncSession,
    job_id: UUID,
    guard_status: str | None = None,
) -> int:
    """Transition job to cancelled state.

    H3: If guard_status is provided (e.g. 'queued'), the UPDATE is only applied
    when the current status matches — preventing race-condition overwrites.
    Returns the number of rows updated (0 = already transitioned, no-op).
    """
    stmt = update(Job).where(Job.id == job_id)
    if guard_status is not None:
        stmt = stmt.where(Job.status == guard_status)
    cursor: CursorResult[tuple[()]] = await session.execute(  # type: ignore[assignment]
        stmt.values(status="cancelled", finished_at=_now())
    )
    return int(cursor.rowcount)


async def list_orphans(session: AsyncSession) -> list[Job]:
    """Return all jobs stuck in 'running' state (worker crash recovery)."""
    result = await session.execute(select(Job).where(Job.status == "running"))
    return list(result.scalars().all())


async def list_active_job_ids(session: AsyncSession) -> set[str]:
    """Return string UUIDs of all queued or running jobs.

    Used by the janitor to skip workspace dirs that belong to active jobs.
    Returns a set of str(uuid) for O(1) membership checks.
    """
    result = await session.execute(select(Job.id).where(Job.status.in_(["queued", "running"])))
    return {str(row[0]) for row in result.all()}
