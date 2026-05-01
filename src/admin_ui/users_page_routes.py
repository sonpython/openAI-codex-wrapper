"""
Admin UI — Users page handlers.

Sub-router included by routes.py (prefix /admin/ui already set there).
Session auth enforced via dependency passed at include time.

Routes:
  GET /users              — users list page (table with aggregates + pagination)
  GET /users/_table       — HTMX partial: paginated table rows
  GET /users/{user_id}    — user detail page (keys list + 30d chart)
  GET /users/{user_id}/_chart_data  — JSON for Chart.js 30d daily series
"""

from __future__ import annotations

import structlog
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin_ui.templates_env import templates
from src.db.crud.users import list_with_aggregates
from src.db.engine import get_session
from src.db.models import ApiKey, User

logger = structlog.get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/users", response_class=HTMLResponse)
async def get_users_page(
    request: Request,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """Users list page — table with current-month aggregates + pagination."""
    limit = min(limit, 500)
    users, total = await list_with_aggregates(session, limit=limit, offset=offset)

    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "users": users,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )


@router.get("/users/_table", response_class=HTMLResponse)
async def get_users_table_partial(
    request: Request,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """HTMX partial — paginated table body rows for users list."""
    limit = min(limit, 500)
    users, total = await list_with_aggregates(session, limit=limit, offset=offset)

    return templates.TemplateResponse(
        request,
        "partials/users_row.html",
        {
            "users": users,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def get_user_detail(
    user_id: UUID,
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """User detail page — keys list + 30d Chart.js line chart."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")

    keys_result = await session.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc())
    )
    keys = keys_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "user_detail.html",
        {
            "user": user,
            "keys": keys,
            "chart_data_url": f"/admin/ui/users/{user_id}/_chart_data",
        },
    )


@router.get("/users/{user_id}/_chart_data")
async def get_user_chart_data(
    user_id: UUID,
    request: Request,
    session: SessionDep,
    range: Annotated[str, Query()] = "30d",
) -> JSONResponse:
    """JSON endpoint: 30d daily usage series for Chart.js.

    Queries jobs table for this user, groups by UTC day.
    Returns {"labels": [...], "requests": [...], "tokens": [...]}.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415
    from sqlalchemy import func  # noqa: PLC0415
    from src.db.models import Job  # noqa: PLC0415

    _valid = {"24h", "7d", "30d"}
    if range not in _valid:
        return JSONResponse(
            status_code=400,
            content={"detail": f"range must be one of {sorted(_valid)}"},
        )

    hours = {"24h": 24, "7d": 168, "30d": 720}[range]
    since = datetime.now(UTC) - timedelta(hours=hours)

    day_expr = func.date_trunc("day", func.timezone("UTC", Job.enqueued_at))

    try:
        result = await session.execute(
            select(
                day_expr.label("day"),
                func.count(Job.id).label("requests"),
            )
            .where(Job.user_id == user_id, Job.enqueued_at >= since)
            .group_by(day_expr)
            .order_by(day_expr)
        )
        rows = result.all()
    except Exception:
        logger.warning("admin_ui.users.chart_query_failed", user_id=str(user_id), exc_info=True)
        rows = []

    labels = [row.day.strftime("%Y-%m-%d") for row in rows]
    requests = [int(row.requests) for row in rows]
    tokens = [0] * len(rows)  # daily token breakdown requires jobs.token_count column

    return JSONResponse(
        content={"labels": labels, "requests": requests, "tokens": tokens}
    )
