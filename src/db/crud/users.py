"""
CRUD helpers for the users table.

Thin async helpers that wrap SQLAlchemy ORM calls.
All functions accept an AsyncSession and return typed ORM instances.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import User


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
