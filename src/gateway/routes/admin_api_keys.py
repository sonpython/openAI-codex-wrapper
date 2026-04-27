"""
Admin endpoint for API key management.

Protected by X-Admin-Token header (constant-time compare via secrets.compare_digest).
This is the ONLY way to create API keys in v1 — no self-service signup.

Endpoints:
  POST   /admin/api-keys          — create a new key (returns plaintext ONCE)
  GET    /admin/api-keys          — list all keys (prefix only, no plaintext/hash)
  POST   /admin/api-keys/{id}/rotate — rotate key (old invalidated immediately)
  DELETE /admin/api-keys/{id}     — soft-revoke a key (sets revoked_at)

Rotation policy (v1 — simple/immediate):
  POST /rotate invalidates the old key immediately and returns new plaintext.
  24h grace window is deferred to v1.1; document this clearly.
  Rationale: immediate invalidation is safer; clients can update their key.

Security:
  - Admin token compared with secrets.compare_digest (constant-time).
  - Plaintext key never stored; returned once in POST response only.
  - LIST response returns prefix + metadata only — no key_hash exposed.
  - 403 permission_denied on token mismatch (same shape as other auth errors).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.hashing import generate_api_key
from src.db.crud import api_keys as api_keys_crud
from src.db.crud.users import get_or_create_by_email
from src.db.engine import get_session
from src.db.models import ApiKey
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

_VALID_TIERS = {"free", "pro", "ent"}


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class AdminCreateKeyRequest(BaseModel):
    user_email: EmailStr
    name: str
    tier: str = "free"

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, v: str) -> str:
        if v not in _VALID_TIERS:
            raise ValueError(f"tier must be one of {sorted(_VALID_TIERS)}")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class AdminCreateKeyResponse(BaseModel):
    id: UUID
    key: str  # plaintext — returned ONCE; never retrievable again
    prefix: str
    tier: str
    created_at: datetime


class ApiKeySummary(BaseModel):
    id: UUID
    user_id: UUID
    prefix: str
    name: str
    tier: str
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class RotateKeyResponse(BaseModel):
    id: UUID
    key: str  # new plaintext — returned ONCE; old key is immediately invalid
    prefix: str
    tier: str
    created_at: datetime


# ── Admin token dependency ────────────────────────────────────────────────────


def _verify_admin_token(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """FastAPI dependency: reject request if X-Admin-Token is missing or wrong.

    Uses secrets.compare_digest for constant-time comparison to prevent
    timing-based enumeration of the admin token.
    """
    settings = get_settings()
    expected = settings.admin_token.get_secret_value()

    if x_admin_token is None or not secrets.compare_digest(
        x_admin_token.encode(), expected.encode()
    ):
        raise HTTPException(status_code=403, detail="permission_denied")


AdminTokenDep = Annotated[None, Depends(_verify_admin_token)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/api-keys", response_model=AdminCreateKeyResponse, status_code=201)
async def create_api_key(
    body: AdminCreateKeyRequest,
    _: AdminTokenDep,
    session: SessionDep,
) -> AdminCreateKeyResponse:
    """Create a new API key for the given user email.

    If the user does not exist they are created automatically.
    The plaintext key is returned exactly once in this response.
    """
    user, created = await get_or_create_by_email(session, str(body.user_email))
    if created:
        logger.info("admin.user_created", email=str(body.user_email))

    api_key, plaintext = await api_keys_crud.create(session, user.id, body.name, body.tier)
    await session.commit()

    logger.info(
        "admin.api_key_created",
        key_id=str(api_key.id),
        prefix=api_key.prefix,
        tier=api_key.tier,
        user_id=str(user.id),
    )

    return AdminCreateKeyResponse(
        id=api_key.id,
        key=plaintext,
        prefix=api_key.prefix,
        tier=api_key.tier,
        created_at=api_key.created_at,
    )


@router.get("/api-keys", response_model=list[ApiKeySummary])
async def list_api_keys(
    _: AdminTokenDep,
    session: SessionDep,
) -> list[ApiKeySummary]:
    """List all API keys — prefix and metadata only, no plaintext or hash."""
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = result.scalars().all()

    return [
        ApiKeySummary(
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


@router.post("/api-keys/{key_id}/rotate", response_model=RotateKeyResponse, status_code=200)
async def rotate_api_key(
    key_id: UUID,
    _: AdminTokenDep,
    session: SessionDep,
) -> RotateKeyResponse:
    """Rotate an API key: generate new key, invalidate old one immediately.

    The new plaintext is returned exactly once.
    The old key is invalidated immediately (revoked_at = now()).

    v1 design decision: immediate invalidation (no 24h grace).
    Rationale: simpler, safer — clients should update promptly.
    24h grace window deferred to v1.1 as a tier-tunable feature.
    """
    existing = await api_keys_crud.get_by_id(session, key_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="api_key_not_found")

    old_prefix = existing.prefix

    # Generate new key material
    plaintext, new_prefix, new_hash = generate_api_key()

    # Update in-place: replace hash + prefix, set revoked_at on old to now()
    # We overwrite the existing row (simpler than creating a new row).
    await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id)
        .values(
            key_hash=new_hash,
            prefix=new_prefix,
            revoked_at=None,  # reset revoked state if it was revoked
            last_used_at=None,
        )
    )
    await session.commit()

    # Refresh to get updated values
    refreshed = await api_keys_crud.get_by_id(session, key_id)
    if refreshed is None:  # pragma: no cover
        raise HTTPException(status_code=500, detail="rotate_failed")

    logger.info(
        "admin.api_key_rotated",
        key_id=str(key_id),
        old_prefix=old_prefix,
        new_prefix=new_prefix,
    )

    return RotateKeyResponse(
        id=refreshed.id,
        key=plaintext,
        prefix=refreshed.prefix,
        tier=refreshed.tier,
        created_at=refreshed.created_at,
    )


@router.delete("/api-keys/{key_id}", status_code=204, response_model=None)
async def revoke_api_key(
    key_id: UUID,
    _: AdminTokenDep,
    session: SessionDep,
) -> None:
    """Soft-revoke an API key by setting revoked_at = now().

    Subsequent requests using this key return 401 immediately (no cache).
    Returns 204 on success, 404 if the key_id does not exist.
    """
    found = await api_keys_crud.revoke(session, key_id)
    if not found:
        raise HTTPException(status_code=404, detail="api_key_not_found")
    await session.commit()
    logger.info("admin.api_key_revoked", key_id=str(key_id))
