"""
Admin UI — API key management page handlers.

Sub-router included by routes.py (prefix /admin/ui already set there).
All routes require session — enforced via ``require_session`` dependency
passed in from routes.py at include time via ``dependencies=[...]``.

Routes:
  GET    /keys              — list all keys (DB join with users for email)
  POST   /keys/_create      — HTMX: create key, return row partial + raw key once
  POST   /keys/{id}/_rotate — HTMX: rotate key, return updated row partial
  DELETE /keys/{id}         — HTMX: revoke key, return empty (row removed by HTMX)
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin_ui.templates_env import templates
from src.auth.hashing import generate_api_key
from src.db.crud import api_keys as api_keys_crud
from src.db.crud.api_keys import DEFAULT_MODE, VALID_MODES
from src.db.crud.users import get_or_create_by_email
from src.db.engine import get_session
from src.db.models import ApiKey, User

logger = structlog.get_logger(__name__)

router = APIRouter()

_VALID_TIERS = {"free", "pro", "ent", "enterprise"}

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _build_key_dict(k: ApiKey, user_email: str) -> dict[str, Any]:
    return {
        "id": str(k.id),
        "prefix": k.prefix,
        "name": k.name,
        "tier": k.tier,
        "mode": k.mode,
        "user_email": user_email,
        "last_used_at": k.last_used_at,
        "revoked_at": k.revoked_at,
        "created_at": k.created_at,
    }


@router.get("/keys", response_class=HTMLResponse)
async def get_keys_page(
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """Keys management page — list all API keys with user email joined."""
    try:
        result = await session.execute(
            select(ApiKey, User)
            .join(User, ApiKey.user_id == User.id)
            .order_by(ApiKey.created_at.desc())
        )
        keys = [_build_key_dict(k, u.email) for k, u in result.all()]
    except Exception:
        logger.warning("admin_ui.keys.fetch_failed", exc_info=True)
        keys = []

    return templates.TemplateResponse(request, "keys.html", {"keys": keys})


@router.post("/keys/_create", response_class=HTMLResponse)
async def post_create_key(
    request: Request,
    session: SessionDep,
    user_email: Annotated[str, Form()],
    name: Annotated[str, Form()],
    tier: Annotated[str, Form()] = "free",
    mode: Annotated[str, Form()] = DEFAULT_MODE,
) -> HTMLResponse:
    """HTMX: create a new API key, return row partial with raw key shown once."""
    if tier not in _VALID_TIERS:
        return HTMLResponse(
            content=f'<p class="text-red-600 text-sm">Invalid tier: {tier}</p>',
            status_code=400,
        )
    if mode not in VALID_MODES:
        return HTMLResponse(
            content=f'<p class="text-red-600 text-sm">Invalid mode: {mode}</p>',
            status_code=400,
        )
    name = name.strip()
    if not name:
        return HTMLResponse(
            content='<p class="text-red-600 text-sm">Name must not be blank</p>',
            status_code=400,
        )

    try:
        user, _ = await get_or_create_by_email(session, user_email)
        api_key, plaintext = await api_keys_crud.create(session, user.id, name, tier, mode)
        await session.commit()
        logger.info(
            "admin_ui.key_created", key_id=str(api_key.id), tier=api_key.tier, mode=api_key.mode
        )
        return templates.TemplateResponse(
            request,
            "partials/keys_row.html",
            {"key": _build_key_dict(api_key, user_email), "new_plaintext": plaintext},
        )
    except Exception:
        logger.warning("admin_ui.key_create_failed", exc_info=True)
        return HTMLResponse(
            content='<p class="text-red-600 text-sm">Failed to create key. See logs.</p>',
            status_code=500,
        )


@router.post("/keys/{key_id}/_rotate", response_class=HTMLResponse)
async def post_rotate_key(
    key_id: UUID,
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """HTMX: rotate a key, return updated row partial with new raw key shown once."""
    existing = await api_keys_crud.get_by_id(session, key_id)
    if existing is None:
        return HTMLResponse(
            content='<p class="text-red-600 text-sm">Key not found</p>',
            status_code=404,
        )

    user_result = await session.execute(select(User).where(User.id == existing.user_id))
    user = user_result.scalar_one_or_none()
    user_email = user.email if user else "unknown"

    plaintext, new_prefix, new_hash = generate_api_key()

    try:
        await session.execute(
            sa_update(ApiKey)
            .where(ApiKey.id == key_id)
            .values(key_hash=new_hash, prefix=new_prefix, revoked_at=None, last_used_at=None)
        )
        await session.commit()

        refreshed = await api_keys_crud.get_by_id(session, key_id)
        if refreshed is None:
            raise RuntimeError("rotate_failed")

        logger.info("admin_ui.key_rotated", key_id=str(key_id), new_prefix=new_prefix)
        return templates.TemplateResponse(
            request,
            "partials/keys_row.html",
            {"key": _build_key_dict(refreshed, user_email), "new_plaintext": plaintext},
        )
    except Exception:
        logger.warning("admin_ui.key_rotate_failed", key_id=str(key_id), exc_info=True)
        return HTMLResponse(
            content='<p class="text-red-600 text-sm">Failed to rotate key. See logs.</p>',
            status_code=500,
        )


@router.delete("/keys/{key_id}", response_class=HTMLResponse)
async def delete_key(
    key_id: UUID,
    request: Request,
    session: SessionDep,
) -> HTMLResponse:
    """HTMX: revoke a key, return empty (HTMX removes the row via outerHTML swap)."""
    found = await api_keys_crud.revoke(session, key_id)
    if not found:
        return HTMLResponse(
            content='<p class="text-red-600 text-sm">Key not found</p>',
            status_code=404,
        )
    await session.commit()
    logger.info("admin_ui.key_revoked", key_id=str(key_id))
    return HTMLResponse(content="", status_code=200)
