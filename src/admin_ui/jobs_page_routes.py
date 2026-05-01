"""
Admin UI — Job inspector page handlers.

Sub-router included by routes.py (prefix /admin/ui already set there).
Session auth enforced via dependency passed at include time.

Routes:
  GET /jobs              — job inspector page (filter form + table)
  GET /jobs/_table       — HTMX partial: filtered+paginated table body
  GET /jobs/{id}/_detail — HTMX partial: job detail modal (metadata + stderr tail)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from src.admin_ui.templates_env import templates
from src.db.crud import jobs as jobs_crud
from src.db.engine import get_session
from src.db.models import Job

logger = structlog.get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_VALID_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"]


@router.get("/jobs", response_class=HTMLResponse)
async def get_jobs_page(
    request: Request,
    session: SessionDep,
    status: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """Job inspector page — filter form + initial paginated table."""
    limit = min(max(limit, 1), 500)
    items, total = await _fetch_jobs(session, status, user_id, from_, to, limit, offset)

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_filter": status or "",
            "user_id_filter": user_id or "",
            "from_filter": from_ or "",
            "to_filter": to or "",
            "valid_statuses": _VALID_STATUSES,
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )


@router.get("/jobs/_table", response_class=HTMLResponse)
async def get_jobs_table_partial(
    request: Request,
    session: SessionDep,
    status: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """HTMX partial — table body rows for live filter + pagination."""
    limit = min(max(limit, 1), 500)
    items, total = await _fetch_jobs(session, status, user_id, from_, to, limit, offset)

    return templates.TemplateResponse(
        request,
        "partials/jobs_table_fragment.html",
        {
            "jobs": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "status_filter": status or "",
            "user_id_filter": user_id or "",
            "from_filter": from_ or "",
            "to_filter": to or "",
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )


@router.get("/jobs/{job_id}/_detail", response_class=HTMLResponse)
async def get_job_detail_partial(
    job_id: UUID,
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """HTMX partial — job detail modal (metadata + stderr tail from DB)."""
    try:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
    except Exception:
        logger.warning("admin_ui.jobs.detail_fetch_failed", job_id=str(job_id), exc_info=True)
        job = None

    if job is None:
        return HTMLResponse(
            content='<p class="text-red-600 text-sm p-4">Job not found.</p>',
            status_code=404,
        )

    duration_ms: int | None = None
    if job.started_at and job.finished_at:
        duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)

    return templates.TemplateResponse(
        request,
        "partials/jobs_detail_modal.html",
        {
            "job": job,
            "duration_ms": duration_ms,
            "stderr_url": f"/admin/codex/jobs/{job_id}/stderr",
        },
    )


async def _fetch_jobs(
    session: SessionDep,
    status: str | None,
    user_id: str | None,
    from_: str | None,
    to: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    """Parse filter params and call crud helper. Returns (items, total)."""
    parsed_user_id: UUID | None = None
    if user_id:
        try:
            parsed_user_id = UUID(user_id)
        except ValueError:
            pass

    parsed_from: datetime | None = None
    if from_:
        try:
            parsed_from = datetime.fromisoformat(from_.replace("Z", "+00:00"))
        except ValueError:
            pass

    parsed_to: datetime | None = None
    if to:
        try:
            parsed_to = datetime.fromisoformat(to.replace("Z", "+00:00"))
        except ValueError:
            pass

    try:
        return await jobs_crud.list_with_filters(
            session,
            user_id=parsed_user_id,
            status=status or None,
            from_=parsed_from,
            to=parsed_to,
            limit=limit,
            offset=offset,
        )
    except Exception:
        logger.warning("admin_ui.jobs.fetch_failed", exc_info=True)
        return [], 0
