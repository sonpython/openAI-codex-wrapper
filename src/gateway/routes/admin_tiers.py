"""
Admin endpoint for tier (Plan) management.

Protected by X-Admin-Token header (constant-time compare via secrets.compare_digest).

Endpoints:
  GET /admin/tiers              — list all Plan rows
  PUT /admin/tiers/{tier}       — upsert tier limits + invalidate in-process cache

Cache invalidation:
  After each successful PUT, invalidate_cache() is called in the same request
  so the rate-limit middleware picks up the new values on the next request cycle.

Security:
  - Admin token compared with secrets.compare_digest (constant-time).
  - Tier names validated against _VALID_TIERS whitelist to prevent arbitrary rows.
  - All values validated >= 0 via Pydantic Field(ge=0).
"""

from __future__ import annotations

import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.crud import plans as plans_crud
from src.db.crud.plans import invalidate_cache
from src.db.engine import get_session
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

# Whitelist of valid tier names — prevents arbitrary rows entering the plans table.
_VALID_TIERS = {"free", "pro", "ent", "enterprise"}


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class TierEdit(BaseModel):
    """Request body for PUT /admin/tiers/{tier}.

    All values must be >= 0.  A value of 0 means "no limit" in the gateway
    rate-limit middleware; use with caution.
    """

    rpm: int = Field(..., ge=0, description="Requests per minute limit")
    tpm: int = Field(..., ge=0, description="Tokens per minute limit")
    concurrent: int = Field(..., ge=0, description="Max concurrent requests")
    monthly_quota: int = Field(..., ge=0, description="Monthly token quota")


class TierResponse(BaseModel):
    """Response schema for a single tier row."""

    tier: str
    rpm: int
    tpm: int
    concurrent: int
    monthly_tokens: int

    model_config = {"from_attributes": True}


# ── Admin token dependency ────────────────────────────────────────────────────


def _verify_admin_token(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """FastAPI dependency: reject request if X-Admin-Token is missing or wrong."""
    settings = get_settings()
    expected = settings.admin_token.get_secret_value()

    if x_admin_token is None or not secrets.compare_digest(
        x_admin_token.encode(), expected.encode()
    ):
        raise HTTPException(status_code=403, detail="permission_denied")


AdminTokenDep = Annotated[None, Depends(_verify_admin_token)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/tiers", response_model=list[TierResponse])
async def list_tiers(
    _: AdminTokenDep,
    session: SessionDep,
) -> list[TierResponse]:
    """List all tier/plan rows from the database."""
    plans = await plans_crud.list_all(session)
    return [TierResponse.model_validate(p) for p in plans]


@router.put("/tiers/{tier}", response_model=TierResponse)
async def update_tier(
    tier: str,
    body: TierEdit,
    _: AdminTokenDep,
    session: SessionDep,
) -> TierResponse:
    """Upsert rate-limit values for a tier and invalidate the in-process cache.

    The cache invalidation happens within the same request so the next gateway
    request sees the updated values immediately (no stale 5-min window).

    Returns 400 if the tier name is not in the whitelist.
    """
    if tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_tier: must be one of {sorted(_VALID_TIERS)}",
        )

    updated = await plans_crud.update(
        session,
        tier=tier,
        rpm=body.rpm,
        tpm=body.tpm,
        concurrent=body.concurrent,
        monthly_quota=body.monthly_quota,
    )
    await session.commit()

    # Invalidate in-process cache so next request picks up new limits atomically.
    invalidate_cache()

    logger.info(
        "admin.tier_updated",
        tier=tier,
        rpm=body.rpm,
        tpm=body.tpm,
        concurrent=body.concurrent,
        monthly_quota=body.monthly_quota,
    )

    return TierResponse.model_validate(updated)
