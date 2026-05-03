"""
Admin endpoint: paginated audit log with filters.

Protected by X-Admin-Token (same pattern as admin_jobs.py).
Mounted at /admin prefix in app.py → final path: /admin/audit

Endpoint:
  GET /admin/audit?action=&user_id=&from=&to=&limit=&offset=
    - action: exact match on audit_log.action column
    - user_id: UUID filter
    - from / to: ISO 8601 datetime strings
    - limit: default 50, clamped to 1-500
    - offset: default 0
    Returns: {items, total, limit, offset}
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.crud import audit_log as audit_crud
from src.db.engine import get_session
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    id: int
    created_at: datetime
    actor_email: str | None
    action: str | None
    target: str | None
    ip: str | None
    status: int | None
    detail: dict[str, Any] | None

    model_config = {"from_attributes": True}


class PaginatedAudit(BaseModel):
    items: list[AuditEntry]
    total: int
    limit: int
    offset: int


# ── Admin token dependency ────────────────────────────────────────────────────


def _verify_admin_token(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    settings = get_settings()
    expected = settings.admin_token.get_secret_value()
    if x_admin_token is None or not secrets.compare_digest(
        x_admin_token.encode(), expected.encode()
    ):
        raise HTTPException(status_code=403, detail="permission_denied")


AdminTokenDep = Annotated[None, Depends(_verify_admin_token)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Route ─────────────────────────────────────────────────────────────────────


@router.get("/audit", response_model=PaginatedAudit)
async def list_audit(
    _: AdminTokenDep,
    session: SessionDep,
    action: Annotated[str | None, Query()] = None,
    user_id: Annotated[UUID | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedAudit:
    """List audit log entries with optional filters. Limit clamped to 500."""
    limit = min(max(limit, 1), 500)

    try:
        items, total = await audit_crud.list_with_filters(
            session,
            action=action,
            user_id=user_id,
            from_=from_,
            to=to,
            limit=limit,
            offset=offset,
        )
    except Exception as err:
        logger.warning("admin.audit.list_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="internal_error") from err

    return PaginatedAudit(
        items=[AuditEntry(**item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
