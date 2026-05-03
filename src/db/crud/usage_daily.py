"""
CRUD helpers for the usage_daily table.

Provides an atomic upsert helper for daily token/request accounting.
Called from:
  - UsageTrackingMiddleware as a fire-and-forget background task after each
    successful (2xx) chat/responses request.
  - Worker (job_handlers.py) after mark_succeeded to record async job tokens.

The upsert is intentionally idempotent: duplicate calls increment the counters
rather than replacing them (INSERT ... ON CONFLICT DO UPDATE with additive SET).
"""

from __future__ import annotations

import datetime
from uuid import UUID

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models_usage_daily import UsageDaily

logger = structlog.get_logger(__name__)


async def upsert(
    session: AsyncSession,
    *,
    user_id: UUID,
    api_key_id: UUID,
    period: datetime.date,
    requests: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Atomic upsert of daily usage counts.

    Uses PostgreSQL INSERT … ON CONFLICT DO UPDATE to atomically increment
    all counters. If the row does not exist it is created.

    Args:
        session:       SQLAlchemy async session (background pool recommended).
        user_id:       UUID of the authenticated user.
        api_key_id:    UUID of the API key used for the request.
        period:        UTC calendar day (date object).
        requests:      Number of requests to add (usually 1).
        input_tokens:  Prompt tokens consumed.
        output_tokens: Completion tokens generated.
    """
    stmt = (
        pg_insert(UsageDaily)
        .values(
            user_id=user_id,
            api_key_id=api_key_id,
            period=period,
            requests=requests,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "api_key_id", "period"],
            set_={
                "requests": UsageDaily.requests + requests,
                "input_tokens": UsageDaily.input_tokens + input_tokens,
                "output_tokens": UsageDaily.output_tokens + output_tokens,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()
