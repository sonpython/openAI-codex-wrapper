"""
UsageDaily ORM model — phase 09.

Kept in a separate module so models.py stays under 200 LOC.
models.py imports this at its bottom (after Base is defined), so
importing Base from models here is safe — no circular-import issue.
Alembic discovers usage_daily via Base.metadata because env.py imports
src.db.models, which triggers this module.

Table: usage_daily
  Composite PK: (user_id, api_key_id, period)
  Written by:
    - UsageTrackingMiddleware on each chat/responses request (best-effort)
    - Worker on async job completion (mark_succeeded)
  Indexes:
    ix_usage_daily_user_period   (user_id, period)
    ix_usage_daily_api_key_period (api_key_id, period)
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import BigInteger, Date, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

# Base lives in src.db.base (not models.py) to avoid a circular import:
#   models.py re-exports UsageDaily → models_usage_daily → models.py (cycle!)
# Both models.py and this module now independently import from src.db.base.
from src.db.base import Base


class UsageDaily(Base):
    """Per-(user, api_key, day) request + token aggregates.

    Single source of truth for daily usage time-series. Written by:
      - UsageTrackingMiddleware on each chat/responses request
      - Worker on async job completion (mark_succeeded)

    Composite PK (user_id, api_key_id, period) — api_key_id NOT NULL since
    auth middleware always sets it before usage middleware runs.

    api_keys uses soft-delete (revoked_at), so ondelete=RESTRICT is safe —
    keys aren't actually deleted, only revoked.
    """

    __tablename__ = "usage_daily"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, nullable=False
    )
    api_key_id: Mapped[UUID] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT"), primary_key=True, nullable=False
    )
    period: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # Index on (user_id, period) for user time-series queries
    # Index on (api_key_id, period) for per-key queries
    __table_args__ = (
        Index("ix_usage_daily_user_period", "user_id", "period"),
        Index("ix_usage_daily_api_key_period", "api_key_id", "period"),
    )

    def __repr__(self) -> str:
        return (
            f"UsageDaily(user_id={self.user_id!s}, api_key_id={self.api_key_id!s}, "
            f"period={self.period!s})"
        )
