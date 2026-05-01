"""
Admin endpoint: paginated user list with aggregates.

Protected by X-Admin-Token (same pattern as admin_api_keys.py).
Mounted at /admin prefix in app.py → final paths:
  GET /admin/users                    — list users with current-month aggregates
  GET /admin/users/{user_id}/keys     — keys filtered by user

Aggregates (single SQL, no N+1):
  - key_count: COUNT(api_keys) per user
  - current_month_requests: usage_counter.requests WHERE period = first-of-month-UTC
  - current_month_tokens: input_tokens + output_tokens for same period
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.crud.users import list_with_aggregates
from src.db.engine import get_session
from src.db.models import ApiKey
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class UserAggregateResponse(BaseModel):
    id: UUID
    email: str
    created_at: datetime
    key_count: int
    current_month_requests: int
    current_month_tokens: int


class UserListResponse(BaseModel):
    items: list[UserAggregateResponse]
    total: int
    limit: int
    offset: int


class UserKeyResponse(BaseModel):
    id: UUID
    user_id: UUID
    prefix: str
    name: str
    tier: str
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


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


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/users", response_model=UserListResponse)
async def list_users(
    _: AdminTokenDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListResponse:
    """List users with current-month usage aggregates (single SQL, no N+1)."""
    limit = min(limit, 500)
    rows, total = await list_with_aggregates(session, limit=limit, offset=offset)

    return UserListResponse(
        items=[
            UserAggregateResponse(
                id=r.id,
                email=r.email,
                created_at=r.created_at,
                key_count=r.key_count,
                current_month_requests=r.current_month_requests,
                current_month_tokens=r.current_month_tokens,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/users/{user_id}/keys", response_model=list[UserKeyResponse])
async def list_user_keys(
    user_id: UUID,
    _: AdminTokenDep,
    session: SessionDep,
) -> list[UserKeyResponse]:
    """List all API keys for a specific user."""
    result = await session.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()

    return [
        UserKeyResponse(
            id=k.id,
            user_id=k.user_id,
            prefix=k.prefix,
            name=k.name,
            tier=k.tier,
            last_used_at=k.last_used_at,
            revoked_at=k.revoked_at,
            created_at=k.created_at,
        )
        for k in keys
    ]
