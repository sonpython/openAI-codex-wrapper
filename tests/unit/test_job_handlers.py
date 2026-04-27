"""
Unit tests for workers/job_handlers.py run_codex_job.

Mocks: DB session (bg_session), Redis, CodexRunner, git_clone, git_diff,
workspace create/cleanup. No real subprocesses or DB required.

Covers:
  - Early-cancel path (cancel flag set before worker starts)
  - Job not found in DB
  - Clone failure → mark_failed
  - Successful path → mark_succeeded + terminal event published
  - Cancel mid-run (flag set during codex iteration) → mark_cancelled
  - Workspace cleanup runs on every exit path (success, fail, cancel)
  - Orphan recovery marks running jobs failed
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.workers.git_clone import GitCloneError  # noqa: E402
from src.workers.job_handlers import recover_orphan_jobs, run_codex_job  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_redis(cancel: bool = False) -> MagicMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1" if cancel else None)
    return redis


def _make_job(status: str = "queued", job_id: uuid.UUID | None = None) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.user_id = uuid.uuid4()
    job.status = status
    job.repo_url = "https://github.com/openai/codex"
    job.branch = "main"
    job.task = "Fix the bug"
    job.mode = "read-only"
    return job


def _make_ctx(redis: Any | None = None) -> dict[str, Any]:
    return {"redis": redis or _make_redis()}


def _make_diff_result(files: list[str] | None = None) -> MagicMock:
    from src.workers.git_diff import DiffResult  # noqa: PLC0415

    return DiffResult(
        diff_blob="diff --git a/f.py b/f.py\n+fix",
        diff_size_bytes=30,
        diff_truncated=False,
        files_changed=files or ["f.py"],
    )


def _make_session_cm(job: MagicMock | None = None) -> MagicMock:
    """Return a context manager mock that yields a session mock."""
    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ── Early-cancel path ─────────────────────────────────────────────────────────


async def test_early_cancel_before_start() -> None:
    """Cancel flag set → job marked cancelled without clone or codex."""
    job_id = str(uuid.uuid4())
    redis = _make_redis(cancel=True)  # flag already set
    ctx = _make_ctx(redis)

    mock_cm = _make_session_cm()
    mock_cancel = AsyncMock()
    mock_publish = AsyncMock()

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.mark_cancelled", mock_cancel),
        patch("src.workers.job_handlers.publish_job_event", mock_publish),
        patch("src.workers.job_handlers.make_workspace") as mock_ws,
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "cancelled"
    mock_cancel.assert_awaited_once()
    mock_ws.assert_not_called()  # workspace never created


# ── Job not found ─────────────────────────────────────────────────────────────


async def test_job_not_found_returns_error() -> None:
    job_id = str(uuid.uuid4())
    ctx = _make_ctx()

    mock_cm = _make_session_cm()
    mock_session = mock_cm.__aenter__.return_value
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=None)),
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "error"
    assert result["reason"] == "job_not_found"


# ── Clone failure path ────────────────────────────────────────────────────────


async def test_clone_failure_marks_failed_and_cleans_workspace(tmp_path: Path) -> None:
    job = _make_job()
    job_id = str(job.id)
    ctx = _make_ctx()

    ws_path = tmp_path / job_id
    mock_cm = _make_session_cm()
    mock_mark_running = AsyncMock()
    mock_mark_failed = AsyncMock()
    mock_publish = AsyncMock()
    mock_cleanup = MagicMock()

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=job)),
        patch("src.workers.job_handlers.jobs_crud.mark_running", mock_mark_running),
        patch("src.workers.job_handlers.jobs_crud.mark_failed", mock_mark_failed),
        patch("src.workers.job_handlers.publish_job_event", mock_publish),
        patch("src.workers.job_handlers.make_workspace", return_value=ws_path),
        patch("src.workers.job_handlers.cleanup_workspace", mock_cleanup),
        patch(
            "src.workers.job_handlers.git_clone", AsyncMock(side_effect=GitCloneError("timeout"))
        ),
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "failed"
    assert result["reason"] == "clone_failed"
    mock_mark_failed.assert_awaited_once()
    # Workspace must still be cleaned up
    mock_cleanup.assert_called_once_with(ws_path)


# ── Success path ──────────────────────────────────────────────────────────────


async def test_success_path_marks_succeeded_and_publishes_terminal(tmp_path: Path) -> None:
    job = _make_job()
    job_id = str(job.id)
    ctx = _make_ctx()
    ws_path = tmp_path / job_id

    from src.codex.events import AgentMessageItem, ItemCompleted, TurnCompleted  # noqa: PLC0415

    async def _fake_run_codex(*args: Any, **kwargs: Any):  # type: ignore[return]
        yield ItemCompleted(
            type="item.completed",
            item=AgentMessageItem(type="agent_message", id="i1", text="summary text"),
        )
        yield TurnCompleted(type="turn.completed")

    diff_result = _make_diff_result()
    mock_cm = _make_session_cm()
    mock_mark_running = AsyncMock()
    mock_mark_succeeded = AsyncMock()
    mock_publish = AsyncMock()
    mock_cleanup = MagicMock()

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=job)),
        patch("src.workers.job_handlers.jobs_crud.mark_running", mock_mark_running),
        patch("src.workers.job_handlers.jobs_crud.mark_succeeded", mock_mark_succeeded),
        patch("src.workers.job_handlers.publish_job_event", mock_publish),
        patch("src.workers.job_handlers.make_workspace", return_value=ws_path),
        patch("src.workers.job_handlers.cleanup_workspace", mock_cleanup),
        patch("src.workers.job_handlers.git_clone", AsyncMock(return_value=(True, ""))),
        patch("src.workers.job_handlers.git_rev_parse_head", AsyncMock(return_value="abc123")),
        patch("src.workers.job_handlers.capture_diff", AsyncMock(return_value=diff_result)),
        patch("src.workers.job_handlers.run_codex", _fake_run_codex),
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "succeeded"
    mock_mark_succeeded.assert_awaited_once()
    # Verify job.completed was published
    published_types = [c.args[2] for c in mock_publish.await_args_list]
    assert "job.completed" in published_types
    mock_cleanup.assert_called_once_with(ws_path)


# ── Cancel mid-run ────────────────────────────────────────────────────────────


async def test_cancel_mid_run_marks_cancelled_and_cleans_workspace(tmp_path: Path) -> None:
    job = _make_job()
    job_id = str(job.id)
    ws_path = tmp_path / job_id

    # Redis returns None first (no cancel yet), then "1" (cancel set)
    call_count = 0

    async def _redis_get(key: str) -> bytes | None:
        nonlocal call_count
        call_count += 1
        # First call: early-cancel check → None
        # Subsequent calls: codex loop → signal cancel
        return None if call_count <= 1 else b"1"

    redis = AsyncMock()
    redis.get = _redis_get
    ctx = _make_ctx(redis)

    from src.codex.events import TurnCompleted  # noqa: PLC0415

    async def _slow_codex(*args: Any, **kwargs: Any):  # type: ignore[return]
        yield TurnCompleted(type="turn.completed")

    mock_cm = _make_session_cm()
    mock_mark_running = AsyncMock()
    mock_mark_cancelled = AsyncMock()
    mock_publish = AsyncMock()
    mock_cleanup = MagicMock()

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=job)),
        patch("src.workers.job_handlers.jobs_crud.mark_running", mock_mark_running),
        patch("src.workers.job_handlers.jobs_crud.mark_cancelled", mock_mark_cancelled),
        patch("src.workers.job_handlers.publish_job_event", mock_publish),
        patch("src.workers.job_handlers.make_workspace", return_value=ws_path),
        patch("src.workers.job_handlers.cleanup_workspace", mock_cleanup),
        patch("src.workers.job_handlers.git_clone", AsyncMock(return_value=(True, ""))),
        patch("src.workers.job_handlers.git_rev_parse_head", AsyncMock(return_value="abc")),
        patch("src.workers.job_handlers.run_codex", _slow_codex),
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "cancelled"
    mock_mark_cancelled.assert_awaited_once()
    published_types = [c.args[2] for c in mock_publish.await_args_list]
    assert "job.cancelled" in published_types
    mock_cleanup.assert_called_once_with(ws_path)


# ── Workspace cleanup on unhandled error ──────────────────────────────────────


async def test_workspace_cleanup_on_unexpected_error(tmp_path: Path) -> None:
    job = _make_job()
    job_id = str(job.id)
    ws_path = tmp_path / job_id

    mock_cm = _make_session_cm()
    mock_mark_failed = AsyncMock()
    mock_publish = AsyncMock()
    mock_cleanup = MagicMock()

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("unexpected!")

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=job)),
        patch("src.workers.job_handlers.jobs_crud.mark_running", AsyncMock()),
        patch("src.workers.job_handlers.jobs_crud.mark_failed", mock_mark_failed),
        patch("src.workers.job_handlers.publish_job_event", mock_publish),
        patch("src.workers.job_handlers.make_workspace", return_value=ws_path),
        patch("src.workers.job_handlers.cleanup_workspace", mock_cleanup),
        patch("src.workers.job_handlers.git_clone", _boom),
    ):
        await run_codex_job(_make_ctx(), job_id)

    # mark_failed called (clone raised, which triggers generic except)
    # cleanup always runs
    mock_cleanup.assert_called_once_with(ws_path)


# ── Orphan recovery ───────────────────────────────────────────────────────────


async def test_recover_orphan_jobs_marks_failed() -> None:
    orphan1 = _make_job(status="running")
    orphan2 = _make_job(status="running")

    session = AsyncMock()
    redis = AsyncMock()
    pipe = MagicMock()
    pipe.rpush = MagicMock()
    pipe.expire = MagicMock()
    pipe.publish = MagicMock()
    pipe.execute = AsyncMock(return_value=[1, True, 1])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    redis.pipeline = MagicMock(return_value=pipe)

    mock_list_orphans = AsyncMock(return_value=[orphan1, orphan2])
    mock_mark_failed = AsyncMock()

    with (
        patch("src.workers.job_handlers.jobs_crud.list_orphans", mock_list_orphans),
        patch("src.workers.job_handlers.jobs_crud.mark_failed", mock_mark_failed),
    ):
        await recover_orphan_jobs(session, redis)

    assert mock_mark_failed.await_count == 2
    # Both orphans should be marked with worker_restarted code
    for c in mock_mark_failed.await_args_list:
        assert c.kwargs.get("error_code") == "worker_restarted"
