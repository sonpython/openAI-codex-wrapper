"""
CRUD helpers for the users table.

Thin async helpers that wrap SQLAlchemy ORM calls.
All functions accept an AsyncSession and return typed ORM instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ApiKey, UsageCounter, User


@dataclass
class UserAggregate:
    """User row with derived aggregates for the admin users list."""

    id: UUID
    email: str
    created_at: datetime
    key_count: int
    current_month_requests: int
    current_month_tokens: int  # sum of input_tokens + output_tokens


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Return the User with the given email, or None if not found.

    Emails are stored lowercase (see get_or_create_by_email). Pass a
    pre-normalised address or call .lower() before lookup if in doubt.
    """
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_or_create_by_email(session: AsyncSession, email: str) -> tuple[User, bool]:
    """Fetch an existing User by email, or create one if absent.

    Returns:
        (user, created) where created=True means a new row was inserted.

    Emails are normalised to lowercase before insert/lookup. This ensures that
    "Alice@X.com" and "alice@x.com" resolve to the same user row, matching the
    real-world expectation that email addresses are case-insensitive in practice.

    Note: no upsert — uses SELECT then INSERT to keep logic readable.
    Race condition on concurrent creation is tolerated: the second INSERT will
    raise IntegrityError (unique constraint); callers should handle if needed.
    For v1 admin-only issuance this race is essentially impossible.
    """
    # Normalise: strip whitespace + lowercase for case-insensitive storage.
    email = email.strip().lower()

    user = await get_by_email(session, email)
    if user is not None:
        return user, False

    user = User(email=email)
    session.add(user)
    await session.flush()  # populate id + server_default created_at
    return user, True


async def list_with_aggregates(
    session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[UserAggregate], int]:
    """Return paginated users with key_count and current-month usage aggregates.

    Single SQL query using LEFT JOIN + subquery to avoid N+1:
      - key_count: COUNT(api_keys) per user
      - current_month_requests/tokens: from usage_counter WHERE period = first-of-month-UTC

    Returns (rows, total_count).
    """
    today = datetime.now(timezone.utc).date()
    current_period = date(today.year, today.month, 1)

    # Subquery: key count per user
    key_count_subq = (
        select(ApiKey.user_id, func.count(ApiKey.id).label("key_count"))
        .group_by(ApiKey.user_id)
        .subquery()
    )

    # Subquery: current-month usage per user
    usage_subq = (
        select(
            UsageCounter.user_id,
            UsageCounter.requests.label("requests"),
            (UsageCounter.input_tokens + UsageCounter.output_tokens).label("total_tokens"),
        )
        .where(UsageCounter.period == current_period)
        .subquery()
    )

    base_query = (
        select(
            User.id,
            User.email,
            User.created_at,
            func.coalesce(key_count_subq.c.key_count, 0).label("key_count"),
            func.coalesce(usage_subq.c.requests, 0).label("current_month_requests"),
            func.coalesce(usage_subq.c.total_tokens, 0).label("current_month_tokens"),
        )
        .outerjoin(key_count_subq, User.id == key_count_subq.c.user_id)
        .outerjoin(usage_subq, User.id == usage_subq.c.user_id)
        .order_by(User.created_at.desc())
    )

    # Total count
    count_result = await session.execute(
        select(func.count()).select_from(User)
    )
    total: int = count_result.scalar_one()

    # Paginated rows
    result = await session.execute(base_query.limit(limit).offset(offset))
    rows = result.all()

    aggregates = [
        UserAggregate(
            id=row.id,
            email=row.email,
            created_at=row.created_at,
            key_count=int(row.key_count),
            current_month_requests=int(row.current_month_requests),
            current_month_tokens=int(row.current_month_tokens),
        )
        for row in rows
    ]
    return aggregates, total
