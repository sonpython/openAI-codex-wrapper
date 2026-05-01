"""
Admin endpoint: per-user and per-key usage daily time series.

Protected by X-Admin-Token (same pattern as admin_api_keys.py).
Mounted at /admin prefix in app.py → final paths:
  GET /admin/usage/summary?user_id=&range=24h|7d|30d
  GET /admin/usage/by-key/{key_id}?range=24h|7d|30d

Both endpoints query the usage_daily table, GROUP BY period (UTC date).
Range parameter is validated; invalid values return 400.

Response shape:
  [{"day": "2026-04-25", "requests": N, "tokens": N}, ...]
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.db.models import UsageDaily  # re-exported by models.py from models_usage_daily
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

_VALID_RANGES = {"24h", "7d", "30d"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class DailyUsage(BaseModel):
    day: str  # ISO date "2026-04-25"
    requests: int
    tokens: int


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


# ── Helpers ───────────────────────────────────────────────────────────────────


def parse_range(range_str: str) -> timedelta:
    """Parse range string to timedelta. Raises ValueError on invalid input."""
    if range_str not in _VALID_RANGES:
        raise ValueError(f"range must be one of {sorted(_VALID_RANGES)}")
    if range_str == "24h":
        return timedelta(hours=24)
    if range_str == "7d":
        return timedelta(days=7)
    return timedelta(days=30)


async def _query_daily_series(
    session: AsyncSession,
    since: datetime,
    user_id: UUID | None = None,
    api_key_id: UUID | None = None,
) -> list[DailyUsage]:
    """Query usage_daily table for daily aggregates. Returns sorted ascending by day.

    Filters by period >= since.date() so we use the indexed Date column directly
    (no expensive date_trunc on a timestamp). Covers all request types:
    chat completions, responses API, and async jobs.

    Token totals are summed from input_tokens + output_tokens per day.
    """
    since_date: date = since.date()

    stmt = (
        select(
            UsageDaily.period.label("day"),
            func.sum(UsageDaily.requests).label("requests"),
            func.sum(UsageDaily.input_tokens + UsageDaily.output_tokens).label("tokens"),
        )
        .where(UsageDaily.period >= since_date)
        .group_by(UsageDaily.period)
        .order_by(UsageDaily.period)
    )

    if user_id is not None:
        stmt = stmt.where(UsageDaily.user_id == user_id)
    if api_key_id is not None:
        stmt = stmt.where(UsageDaily.api_key_id == api_key_id)

    try:
        result = await session.execute(stmt)
        rows = result.all()
    except Exception:
        logger.warning("admin_usage.query_failed", exc_info=True)
        return []

    return [
        DailyUsage(
            day=row.day.strftime("%Y-%m-%d") if hasattr(row.day, "strftime") else str(row.day),
            requests=int(row.requests or 0),
            tokens=int(row.tokens or 0),
        )
        for row in rows
    ]


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/usage/summary", response_model=list[DailyUsage])
async def get_usage_summary(
    _: AdminTokenDep,
    session: SessionDep,
    range: Annotated[str, Query()] = "7d",
    user_id: Annotated[str | None, Query()] = None,
) -> list[DailyUsage]:
    """Daily usage series from usage_daily table, optionally filtered by user.

    range: "24h" | "7d" | "30d" — invalid value returns 400.
    user_id: optional UUID string filter.
    """
    try:
        delta = parse_range(range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since = datetime.now(UTC) - delta

    parsed_uid: UUID | None = None
    if user_id:
        try:
            parsed_uid = UUID(user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="user_id must be a valid UUID") from exc

    return await _query_daily_series(session, since, user_id=parsed_uid)


@router.get("/usage/by-key/{key_id}", response_model=list[DailyUsage])
async def get_usage_by_key(
    key_id: UUID,
    _: AdminTokenDep,
    session: SessionDep,
    range: Annotated[str, Query()] = "7d",
) -> list[DailyUsage]:
    """Daily usage series filtered by API key ID."""
    try:
        delta = parse_range(range)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since = datetime.now(UTC) - delta
    return await _query_daily_series(session, since, api_key_id=key_id)
