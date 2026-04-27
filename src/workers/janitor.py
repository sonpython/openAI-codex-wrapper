"""
Workspace janitor — Arq cron task.

Runs every 10 minutes. Scans WORKSPACE_ROOT for stale directories:
  - Not referenced by an active (queued|running) job in Postgres
  - mtime older than 1 hour

Also runs the daily audit_log retention purge (called from WorkerSettings cron).

Safety:
  - All cleanup paths go through validate_path_inside() to prevent traversal.
  - shutil.rmtree uses ignore_errors=True — partial cleanup is fine.
  - Active-jobs query is authoritative; mtime > 1h is a secondary safety buffer.
  - Janitor failure never crashes the worker (all exceptions are caught).
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import structlog

from src.codex.workspace import validate_path_inside
from src.db.crud import jobs as jobs_crud
from src.db.crud.audit_log import purge_old as purge_audit_log
from src.db.engine import bg_session
from src.settings import get_settings

logger = structlog.get_logger(__name__)

_STALE_AGE_SECONDS = 3600  # 1 hour


async def cleanup_stale_workspaces(ctx: dict[str, Any]) -> dict[str, int]:
    """Arq cron task: remove orphaned workspace directories.

    A workspace is stale when:
      1. No job with status queued|running references it, AND
      2. Its mtime is older than _STALE_AGE_SECONDS.

    Returns dict with 'cleaned' count.
    """
    settings = get_settings()
    workspace_root = settings.workspace_root

    if not os.path.isdir(workspace_root):
        logger.debug("janitor.workspace_root_missing", path=workspace_root)
        return {"cleaned": 0}

    # Fetch active job IDs once — authoritative source of truth.
    try:
        async with bg_session() as session:
            active_ids: set[str] = await jobs_crud.list_active_job_ids(session)
    except Exception:
        logger.warning("janitor.db_fetch_failed", exc_info=True)
        return {"cleaned": 0}

    now = time.time()
    cleaned = 0

    try:
        entries = list(os.scandir(workspace_root))
    except OSError:
        logger.warning("janitor.scandir_failed", path=workspace_root, exc_info=True)
        return {"cleaned": 0}

    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue

        # Skip if this dir belongs to an active job
        if entry.name in active_ids:
            continue

        # Skip if recently modified (still being set up or torn down)
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue

        if (now - mtime) < _STALE_AGE_SECONDS:
            continue

        # Validate path before deletion (traversal guard)
        try:
            safe_path = validate_path_inside(Path(workspace_root), Path(entry.path))
        except Exception:
            logger.warning("janitor.path_validation_failed", path=entry.path)
            continue

        shutil.rmtree(safe_path, ignore_errors=True)
        logger.info("janitor.cleaned", path=entry.name, age_seconds=int(now - mtime))
        cleaned += 1

    logger.info("janitor.run_complete", cleaned=cleaned, checked=len(entries))
    return {"cleaned": cleaned}


async def purge_old_audit_logs(ctx: dict[str, Any]) -> dict[str, int]:
    """Arq cron task: delete audit_log rows beyond retention window.

    Default retention: settings.audit_log_retention_days (90 days).
    """
    settings = get_settings()
    deleted = await purge_audit_log(settings.audit_log_retention_days)
    return {"deleted": deleted}
