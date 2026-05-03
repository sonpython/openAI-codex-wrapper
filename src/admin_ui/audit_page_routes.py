"""
Admin UI — Audit log viewer page handlers.

Sub-router included by routes.py (prefix /admin/ui already set there).
Session auth enforced via dependency passed at include time.

Routes:
  GET /audit        — audit viewer page (filter form + table)
  GET /audit/_table — HTMX partial: filtered+paginated table body
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin_ui.templates_env import templates
from src.db.crud import audit_log as audit_crud
from src.db.engine import get_session

logger = structlog.get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/audit", response_class=HTMLResponse)
async def get_audit_page(
    request: Request,
    session: SessionDep,
    action: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """Audit log viewer page — filter form + initial paginated table."""
    limit = min(max(limit, 1), 500)
    items, total = await _fetch_audit(session, action, user_id, from_, to, limit, offset)

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "entries": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "action_filter": action or "",
            "user_id_filter": user_id or "",
            "from_filter": from_ or "",
            "to_filter": to or "",
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )


@router.get("/audit/_table", response_class=HTMLResponse)
async def get_audit_table_partial(
    request: Request,
    session: SessionDep,
    action: Annotated[str | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """HTMX partial — table body rows for live filter + pagination."""
    limit = min(max(limit, 1), 500)
    items, total = await _fetch_audit(session, action, user_id, from_, to, limit, offset)

    return templates.TemplateResponse(
        request,
        "partials/audit_table_fragment.html",
        {
            "entries": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "action_filter": action or "",
            "user_id_filter": user_id or "",
            "from_filter": from_ or "",
            "to_filter": to or "",
            "has_prev": offset > 0,
            "has_next": offset + limit < total,
            "prev_offset": max(0, offset - limit),
            "next_offset": offset + limit,
        },
    )


async def _fetch_audit(
    session: SessionDep,
    action: str | None,
    user_id: str | None,
    from_: str | None,
    to: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    """Parse filter params and call crud helper. Returns (items, total)."""
    parsed_user_id: UUID | None = None
    if user_id:
        with contextlib.suppress(ValueError):
            parsed_user_id = UUID(user_id)

    parsed_from: datetime | None = None
    if from_:
        with contextlib.suppress(ValueError):
            parsed_from = datetime.fromisoformat(from_.replace("Z", "+00:00"))

    parsed_to: datetime | None = None
    if to:
        with contextlib.suppress(ValueError):
            parsed_to = datetime.fromisoformat(to.replace("Z", "+00:00"))

    try:
        return await audit_crud.list_with_filters(
            session,
            action=action or None,
            user_id=parsed_user_id,
            from_=parsed_from,
            to=parsed_to,
            limit=limit,
            offset=offset,
        )
    except Exception:
        logger.warning("admin_ui.audit.fetch_failed", exc_info=True)
        return [], 0
