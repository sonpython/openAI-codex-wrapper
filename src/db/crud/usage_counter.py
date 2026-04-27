"""
CRUD helpers for the usage_counter table.

Provides an upsert helper for monthly token accounting.
Called from UsageTrackingMiddleware as a fire-and-forget background task
after each successful (2xx) response.

The upsert is intentionally idempotent: duplicate calls increment the counters
rather than replacing them (INSERT ... ON CONFLICT DO UPDATE with additive SET).
"""

from __future__ import annotations

import datetime

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import UsageCounter

logger = structlog.get_logger(__name__)


async def increment(
    session: AsyncSession,
    user_id: object,
    period: datetime.date,
    requests: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Upsert monthly usage counters for a user.

    Uses PostgreSQL INSERT ... ON CONFLICT DO UPDATE to atomically increment
    all counters.  If the row does not exist it is created.

    Args:
        session:       SQLAlchemy async session (background pool recommended).
        user_id:       UUID of the authenticated user.
        period:        First day of the UTC calendar month (date object).
        requests:      Number of requests to add (usually 1).
        input_tokens:  Prompt tokens consumed.
        output_tokens: Completion tokens generated.
    """
    stmt = (
        pg_insert(UsageCounter)
        .values(
            user_id=user_id,
            period=period,
            requests=requests,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "period"],
            set_={
                "requests": UsageCounter.requests + requests,
                "input_tokens": UsageCounter.input_tokens + input_tokens,
                "output_tokens": UsageCounter.output_tokens + output_tokens,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()
