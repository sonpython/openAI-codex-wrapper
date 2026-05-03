"""
Admin endpoint: paginated job list with filters.

Protected by X-Admin-Token (same pattern as admin_api_keys.py).
Mounted at /admin prefix in app.py → final path: /admin/jobs

Endpoint:
  GET /admin/jobs?user_id=&status=&from=&to=&limit=&offset=
    - user_id: UUID filter
    - status: exact match (queued|running|succeeded|failed|cancelled)
    - from / to: ISO 8601 datetime strings (URL-safe: 2024-01-01T00:00:00Z)
    - limit: default 50, clamped to 1-500
    - offset: default 0
    Returns: {items, total, limit, offset}
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.crud import jobs as jobs_crud
from src.db.engine import get_session
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class JobSummary(BaseModel):
    id: str
    user_email: str
    status: str
    model: str | None
    created_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    exit_code: int | None
    prompt_hash: str | None

    model_config = {"from_attributes": True}


class PaginatedJobs(BaseModel):
    items: list[JobSummary]
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


@router.get("/jobs", response_model=PaginatedJobs)
async def list_jobs(
    _: AdminTokenDep,
    session: SessionDep,
    user_id: Annotated[UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaginatedJobs:
    """List jobs with optional filters. Limit clamped to 500."""
    limit = min(max(limit, 1), 500)

    try:
        items, total = await jobs_crud.list_with_filters(
            session,
            user_id=user_id,
            status=status,
            from_=from_,
            to=to,
            limit=limit,
            offset=offset,
        )
    except Exception as err:
        logger.warning("admin.jobs.list_failed", exc_info=True)
        raise HTTPException(status_code=500, detail="internal_error") from err

    return PaginatedJobs(
        items=[JobSummary(**item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
