"""
Admin UI — Plan tier editor page handlers.

Sub-router included by routes.py (prefix /admin/ui already set there).
Session auth enforced via dependency passed at include time.

Routes:
  GET /tiers                  — tier table with inline-editable cells
  PUT /tiers/{tier}/_save     — HTMX: upsert tier, invalidate cache, return toast
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from src.admin_ui.templates_env import templates
from src.db.crud import plans as plans_crud
from src.db.crud.plans import invalidate_cache
from src.db.engine import get_session

logger = structlog.get_logger(__name__)

router = APIRouter()

_VALID_TIERS = {"free", "pro", "ent", "enterprise"}

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/tiers", response_class=HTMLResponse)
async def get_tiers_page(
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """Tier editor page — table of plan tiers with editable rate-limit cells."""
    try:
        plans = await plans_crud.list_all(session)
        tiers = [
            {
                "tier": p.tier,
                "rpm": p.rpm,
                "tpm": p.tpm,
                "concurrent": p.concurrent,
                "monthly_tokens": p.monthly_tokens,
            }
            for p in plans
        ]
    except Exception:
        logger.warning("admin_ui.tiers.fetch_failed", exc_info=True)
        tiers = []

    return templates.TemplateResponse(request, "tiers.html", {"tiers": tiers})


@router.put("/tiers/{tier}/_save", response_class=HTMLResponse)
async def put_save_tier(
    tier: str,
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """HTMX: save tier edits, invalidate in-process cache, return toast partial."""
    if tier not in _VALID_TIERS:
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": f"Invalid tier: {tier}", "level": "error"},
            status_code=400,
        )

    try:
        form = await request.form()
        rpm = int(form.get("rpm", 0))  # type: ignore[arg-type]
        tpm = int(form.get("tpm", 0))  # type: ignore[arg-type]
        concurrent = int(form.get("concurrent", 0))  # type: ignore[arg-type]
        monthly_quota = int(form.get("monthly_quota", 0))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": "Invalid numeric values", "level": "error"},
            status_code=400,
        )

    if any(v < 0 for v in [rpm, tpm, concurrent, monthly_quota]):
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": "All values must be >= 0", "level": "error"},
            status_code=400,
        )

    try:
        await plans_crud.update(
            session,
            tier=tier,
            rpm=rpm,
            tpm=tpm,
            concurrent=concurrent,
            monthly_quota=monthly_quota,
        )
        await session.commit()
        # Invalidate in-process cache in same request — next API request sees new limits.
        invalidate_cache()

        logger.info(
            "admin_ui.tier_saved",
            tier=tier,
            rpm=rpm,
            tpm=tpm,
            concurrent=concurrent,
            monthly_quota=monthly_quota,
        )
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": f"Tier '{tier}' saved successfully", "level": "success"},
        )
    except Exception:
        logger.warning("admin_ui.tier_save_failed", tier=tier, exc_info=True)
        return templates.TemplateResponse(
            request,
            "partials/toast.html",
            {"message": "Failed to save tier. See logs.", "level": "error"},
            status_code=500,
        )
