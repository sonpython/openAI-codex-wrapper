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
from typing import Any
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Job, User


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
    api_key_id: UUID | None = None,
) -> Job:
    """Insert a new job row with status=queued and return it."""
    job = Job(
        user_id=user_id,
        api_key_id=api_key_id,
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


async def update_token_counts(
    session: AsyncSession,
    job_id: UUID,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Atomic update of token counts on job completion.

    Called by the worker after codex finishes streaming events.
    Best-effort: if codex doesn't expose token_usage, pass 0 for both.
    """
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(input_tokens=input_tokens, output_tokens=output_tokens)
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


async def list_with_filters(
    session: AsyncSession,
    *,
    user_id: UUID | None = None,
    status: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """List jobs with optional filters, joined with users.email.

    Returns (items, total) where items is a list of dicts and total is the
    unfiltered count matching the filter criteria (for pagination).
    Limit is expected to be pre-clamped by the caller (1-500).
    """
    base = select(Job, User.email).join(User, Job.user_id == User.id)

    if user_id is not None:
        base = base.where(Job.user_id == user_id)
    if status is not None:
        base = base.where(Job.status == status)
    if from_ is not None:
        base = base.where(Job.enqueued_at >= from_)
    if to is not None:
        base = base.where(Job.enqueued_at <= to)

    # Count query
    count_stmt = select(func.count()).select_from(base.subquery())
    total: int = (await session.execute(count_stmt)).scalar_one()

    # Data query with ordering + pagination
    data_stmt = base.order_by(Job.enqueued_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(data_stmt)).all()

    items = []
    for job, user_email in rows:
        duration_ms: int | None = None
        if job.started_at and job.finished_at:
            duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)
        items.append(
            {
                "id": str(job.id),
                "user_email": user_email,
                "status": job.status,
                "model": job.mode,
                "created_at": job.enqueued_at,
                "completed_at": job.finished_at,
                "duration_ms": duration_ms,
                "exit_code": job.exit_code,
                "prompt_hash": None,  # jobs don't store prompt; field kept for schema compat
                "repo_url": job.repo_url,
                "branch": job.branch,
                "error_code": job.error_code,
                "error_message": job.error_message,
                "stderr_tail": job.stderr_tail,
            }
        )

    return items, total


async def list_active_job_ids(session: AsyncSession) -> set[str]:
    """Return string UUIDs of all queued or running jobs.

    Used by the janitor to skip workspace dirs that belong to active jobs.
    Returns a set of str(uuid) for O(1) membership checks.
    """
    result = await session.execute(select(Job.id).where(Job.status.in_(["queued", "running"])))
    return {str(row[0]) for row in result.all()}
