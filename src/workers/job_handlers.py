"""
Arq job handler: run_codex_job.

Lifecycle:
  1. Check early-cancel flag (race: DELETE called before worker started).
  2. Mark running; publish job.started.
  3. Create workspace; git clone repo.
  4. Stream codex events; check cancel flag each iteration.
  5. Capture git diff; mark succeeded; publish terminal events.
  6. On cancel/timeout/error: mark appropriate terminal state.
  7. Always cleanup workspace in finally block.

Cancel mechanism: API sets Redis key ``cancel:job:{id}`` (TTL 300s).
Worker polls it every loop iteration. On detect: SIGTERM→SIGKILL codex
process via runner cancel, then raises asyncio.CancelledError.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.codex.events import ErrorEvent
from src.codex.runner import run_codex
from src.codex.stderr_archive import archive_stderr
from src.codex.workspace import cleanup_workspace, make_workspace
from src.db.crud import jobs as jobs_crud
from src.db.engine import bg_session
from src.observability.metrics import ARQ_JOB_DURATION, ARQ_JOBS_TOTAL
from src.settings import get_settings
from src.workers.event_publisher import publish_job_event
from src.workers.git_clone import GitCloneError, git_clone, git_rev_parse_head
from src.workers.git_diff import capture_diff

logger = structlog.get_logger(__name__)

_CANCEL_KEY_TTL = 300  # seconds — matches API-side SET EX
_STDERR_TAIL_MAX = 4096  # 4KB cap on stored stderr tail


async def _check_cancel(redis: Any, job_id: str) -> bool:
    """Return True if the cancel flag is set in Redis."""
    val = await redis.get(f"cancel:job:{job_id}")
    return val is not None


async def run_codex_job(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Arq task entry point: execute one codex job end-to-end.

    ctx must contain:
        redis — redis.asyncio.Redis client
        db    — AsyncSession (injected by WorkerSettings.on_startup)

    Returns a dict with final status for Arq result storage.
    """
    redis = ctx["redis"]
    settings = get_settings()
    log = logger.bind(job_id=job_id)
    workspace: Path | None = None
    _job_start = time.monotonic()
    _job_outcome = "cancelled"  # updated before each return
    _stderr_bytes: bytes = b""  # accumulated for postmortem archive on failure

    async with bg_session() as session:
        # ── Early-cancel check ────────────────────────────────────────────
        if await _check_cancel(redis, job_id):
            await jobs_crud.mark_cancelled(session, uuid.UUID(job_id))
            await session.commit()
            await publish_job_event(
                redis, job_id, "job.cancelled", {"reason": "cancelled_before_start"}
            )
            log.info("job.cancelled_before_start")
            _job_outcome = "cancelled"
            return {"status": "cancelled"}

        job = await jobs_crud.get_job_unscoped(session, uuid.UUID(job_id))
        if job is None:
            log.error("job.not_found")
            return {"status": "error", "reason": "job_not_found"}

        repo_url = job.repo_url
        branch = job.branch
        task = job.task
        mode = job.mode
        timeout = settings.job_timeout_seconds

    try:
        # ── Mark running ──────────────────────────────────────────────────
        workspace = make_workspace(job_id)
        async with bg_session() as session:
            await jobs_crud.mark_running(session, uuid.UUID(job_id), str(workspace))
            await session.commit()
        await publish_job_event(redis, job_id, "job.started", {})
        log.info("job.started", workspace=str(workspace))

        # ── Git clone ─────────────────────────────────────────────────────
        repo_dir = workspace / "repo"
        try:
            await git_clone(
                repo_url,
                branch,
                repo_dir,
                timeout=float(settings.job_clone_timeout_seconds),
            )
        except GitCloneError as exc:
            async with bg_session() as session:
                await jobs_crud.mark_failed(
                    session,
                    uuid.UUID(job_id),
                    error_code="clone_failed",
                    error_message=str(exc)[:500],
                )
                await session.commit()
            await publish_job_event(redis, job_id, "job.failed", {"error": str(exc)[:200]})
            log.warning("job.clone_failed", error=str(exc))
            _job_outcome = "failed"
            return {"status": "failed", "reason": "clone_failed"}

        head_before = await git_rev_parse_head(repo_dir)

        # ── Stream codex events ───────────────────────────────────────────
        summary_parts: list[str] = []
        stderr_buf: list[str] = []
        exit_code: int | None = 0
        allow_write = mode == "workspace-write"

        async with asyncio.timeout(timeout):
            async for evt in run_codex(
                task,
                allow_write=allow_write,
                workspace_dir=repo_dir,
                timeout=float(timeout),
                request_id=job_id,
            ):
                # Check cancel flag on each event
                if await _check_cancel(redis, job_id):
                    raise asyncio.CancelledError("cancelled by user request")

                evt_dict: dict[str, Any] = evt.model_dump() if hasattr(evt, "model_dump") else {}
                await publish_job_event(redis, job_id, "job.codex_event", {"event": evt_dict})

                # Collect agent message text for summary
                if (
                    hasattr(evt, "item")
                    and hasattr(evt.item, "type")
                    and evt.item.type == "agent_message"
                    and hasattr(evt.item, "text")
                ):
                    summary_parts.append(str(evt.item.text))

                # Capture exit code from error events
                if isinstance(evt, ErrorEvent):
                    stderr_buf.append(evt.error.message)
                    if evt.error.details and "exit_code" in evt.error.details:
                        exit_code = evt.error.details["exit_code"]
                    # Accumulate for postmortem archive
                    _stderr_bytes += evt.error.message.encode("utf-8", errors="replace")

        summary = "\n".join(summary_parts) or None
        stderr_tail = "\n".join(stderr_buf)[-_STDERR_TAIL_MAX:] or None

        # ── Capture diff ──────────────────────────────────────────────────
        diff_result = await capture_diff(repo_dir, head_before)
        if diff_result.files_changed:
            await publish_job_event(
                redis,
                job_id,
                "job.diff_ready",
                {"files_changed": diff_result.files_changed},
            )

        # ── Mark succeeded ────────────────────────────────────────────────
        async with bg_session() as session:
            await jobs_crud.mark_succeeded(
                session,
                uuid.UUID(job_id),
                summary=summary,
                diff_blob=diff_result.diff_blob,
                diff_size_bytes=diff_result.diff_size_bytes,
                files_changed=diff_result.files_changed or None,
                exit_code=exit_code,
                stderr_tail=stderr_tail,
            )
            await session.commit()

        await publish_job_event(redis, job_id, "job.completed", {"summary": summary or ""})
        log.info("job.completed", files_changed=len(diff_result.files_changed))
        _job_outcome = "succeeded"
        return {"status": "succeeded"}

    except asyncio.CancelledError:
        log.info("job.cancelled")
        async with bg_session() as session:
            await jobs_crud.mark_cancelled(session, uuid.UUID(job_id))
            await session.commit()
        await publish_job_event(redis, job_id, "job.cancelled", {})
        _job_outcome = "cancelled"
        return {"status": "cancelled"}

    except TimeoutError:
        log.warning("job.timeout", timeout=timeout)
        async with bg_session() as session:
            await jobs_crud.mark_failed(
                session,
                uuid.UUID(job_id),
                error_code="timeout",
                error_message=f"Job exceeded timeout of {timeout}s",
            )
            await session.commit()
        await publish_job_event(redis, job_id, "job.failed", {"error": "timeout"})
        _job_outcome = "failed"
        return {"status": "failed", "reason": "timeout"}

    except Exception as exc:
        log.exception("job.unexpected_error", error=str(exc))
        async with bg_session() as session:
            await jobs_crud.mark_failed(
                session,
                uuid.UUID(job_id),
                error_code="internal_error",
                error_message=str(exc)[:500],
            )
            await session.commit()
        await publish_job_event(redis, job_id, "job.failed", {"error": str(exc)[:200]})
        _job_outcome = "failed"
        return {"status": "failed", "reason": "internal_error"}

    finally:
        ARQ_JOB_DURATION.labels(outcome=_job_outcome).observe(time.monotonic() - _job_start)
        ARQ_JOBS_TOTAL.labels(status=_job_outcome).inc()
        # MM6: archive stderr before workspace cleanup for failed jobs
        if _job_outcome == "failed" and _stderr_bytes:
            archive_stderr(job_id, _stderr_bytes)
        if workspace is not None:
            cleanup_workspace(workspace)


async def recover_orphan_jobs(session: AsyncSession, redis: Any) -> None:
    """Mark any jobs stuck in 'running' as failed (worker crash recovery).

    Called from WorkerSettings.on_startup before accepting new jobs.
    """
    orphans = await jobs_crud.list_orphans(session)
    for job in orphans:
        await jobs_crud.mark_failed(
            session,
            job.id,
            error_code="worker_restarted",
            error_message="Worker process restarted while job was running.",
        )
        await publish_job_event(
            redis,
            str(job.id),
            "job.failed",
            {"error": "worker_restarted"},
        )
        logger.warning("job.orphan_recovered", job_id=str(job.id))
    if orphans:
        await session.commit()
