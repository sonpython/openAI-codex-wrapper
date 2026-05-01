"""
Audit log CRUD: emit() fire-and-forget + purge_old() retention.

Design:
  - emit() uses the canonical _BG_TASKS pattern (phase-01) with bg_session()
    (pool_size=3, pool_timeout=0.5s). On pool timeout: log WARN + drop — never
    block the request path.
  - prompt is stored as sha256 hash by default (AUDIT_LOG_PROMPT=false).
    Set AUDIT_LOG_PROMPT=true only in dev for raw prompt debugging.
  - All exceptions swallowed inside _persist() — audit MUST NOT break requests.
  - purge_old() deletes rows older than retention_days; called by daily cron.

Schema fields (see AuditLog model):
  request_id, api_key_id, user_id, admin, route, method, status_code,
  duration_ms, codex_cmd, prompt_hash, input_tokens, output_tokens,
  codex_exit_code, error_class, target_id, action
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import bg_session
from src.db.models_audit_log import AuditLog
from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Strong references to in-flight background tasks (C8 pattern from phase-01).
# Without this set, the GC may collect a Task before it completes.
_BG_TASKS: set[asyncio.Task[None]] = set()


def _hash_prompt(prompt: str) -> str:
    """Return sha256 hex digest of the prompt string."""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


async def _persist(fields: dict[str, Any]) -> None:
    """Insert one audit_log row via the background pool.

    Swallows ALL exceptions — this must never propagate to the request path.
    On pool timeout (pool_timeout=0.5s): log WARN + drop.
    """
    try:
        async with bg_session() as session:
            row = AuditLog(**fields)
            session.add(row)
            await session.commit()
    except TimeoutError:
        logger.warning("audit_log.pool_timeout", fields_keys=list(fields.keys()))
    except Exception:
        logger.warning("audit_log.persist_failed", exc_info=True)


def emit(
    *,
    request_id: str | None = None,
    api_key_id: UUID | None = None,
    user_id: UUID | None = None,
    admin: bool = False,
    route: str | None = None,
    method: str | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    codex_cmd: list[str] | None = None,
    prompt: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    codex_exit_code: int | None = None,
    error_class: str | None = None,
    target_id: UUID | None = None,
    action: str | None = None,
) -> None:
    """Schedule an audit log INSERT as a fire-and-forget background task.

    prompt is hashed via sha256 (never stored raw) unless AUDIT_LOG_PROMPT=true.
    Task held in _BG_TASKS to prevent GC before completion.
    """
    settings = get_settings()

    # Compute prompt_hash (or store raw in dev if flag is set)
    prompt_hash: str | None = (
        (prompt[:1024] if settings.audit_log_prompt else _hash_prompt(prompt)) if prompt else None
    )

    fields: dict[str, Any] = {
        "request_id": request_id,
        "api_key_id": api_key_id,
        "user_id": user_id,
        "admin": admin,
        "route": route,
        "method": method,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "codex_cmd": codex_cmd,
        "prompt_hash": prompt_hash,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "codex_exit_code": codex_exit_code,
        "error_class": error_class,
        "target_id": target_id,
        "action": action,
    }
    # Strip None values to avoid inserting NULLs for unset fields
    # (model defaults handle absent columns)
    fields = {k: v for k, v in fields.items() if v is not None}

    task: asyncio.Task[None] = asyncio.create_task(_persist(fields))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def list_with_filters(
    session: AsyncSession,
    *,
    action: str | None = None,
    user_id: UUID | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """List audit_log rows with optional filters.

    Returns (items, total) for pagination.
    Limit is expected to be pre-clamped by the caller (1-500).
    """
    base = select(AuditLog)

    if action is not None:
        base = base.where(AuditLog.action == action)
    if user_id is not None:
        base = base.where(AuditLog.user_id == user_id)
    if from_ is not None:
        base = base.where(AuditLog.created_at >= from_)
    if to is not None:
        base = base.where(AuditLog.created_at <= to)

    count_stmt = select(func.count()).select_from(base.subquery())
    total: int = (await session.execute(count_stmt)).scalar_one()

    data_stmt = base.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(data_stmt)).scalars().all()

    items = []
    for row in rows:
        items.append(
            {
                "id": row.id,
                "created_at": row.created_at,
                "actor_email": None,  # audit_log stores user_id not email; join omitted for perf
                "action": row.action,
                "target": str(row.target_id) if row.target_id else None,
                "ip": None,  # IP not stored in audit_log schema
                "status": row.status_code,
                "detail": {
                    "request_id": row.request_id,
                    "route": row.route,
                    "method": row.method,
                    "duration_ms": row.duration_ms,
                    "user_id": str(row.user_id) if row.user_id else None,
                    "api_key_id": str(row.api_key_id) if row.api_key_id else None,
                    "admin": row.admin,
                    "prompt_hash": row.prompt_hash,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "error_class": row.error_class,
                },
            }
        )

    return items, total


async def purge_old(retention_days: int | None = None) -> int:
    """Delete audit_log rows older than retention_days.

    Returns number of rows deleted. Called by daily cron.
    Uses bg_session to avoid blocking main pool.
    """
    settings = get_settings()
    days = retention_days if retention_days is not None else settings.audit_log_retention_days
    try:
        async with bg_session() as session:
            result = await session.execute(
                delete(AuditLog).where(
                    AuditLog.created_at < text(f"now() - interval '{days} days'")
                )
            )
            await session.commit()
            deleted: int = getattr(result, "rowcount", 0)
            logger.info("audit_log.purged", deleted=deleted, retention_days=days)
            return deleted
    except Exception:
        logger.warning("audit_log.purge_failed", exc_info=True)
        return 0
