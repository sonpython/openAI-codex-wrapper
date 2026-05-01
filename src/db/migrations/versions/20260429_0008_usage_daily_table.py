"""Add usage_daily table for per-day per-key token tracking

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-29 18:00:00.000000+00:00

Creates usage_daily table — the single source of truth for daily usage
time-series across all request types (chat completions, responses, async jobs).

Previously admin_usage.py queried jobs directly, missing chat/responses traffic.
This table is written by UsageTrackingMiddleware (middleware path) and the worker
(job completion path), so all request types are captured.

Composite PK: (user_id, api_key_id, period)
  - period is a UTC calendar date (not a timestamp)
  - api_key_id uses RESTRICT so soft-deleted keys are retained in history

Indexes:
  ix_usage_daily_user_period    (user_id, period)   — user time-series queries
  ix_usage_daily_api_key_period (api_key_id, period) — per-key queries
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_daily",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), sa.ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("requests", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("user_id", "api_key_id", "period"),
    )
    op.create_index("ix_usage_daily_user_period", "usage_daily", ["user_id", "period"])
    op.create_index("ix_usage_daily_api_key_period", "usage_daily", ["api_key_id", "period"])


def downgrade() -> None:
    op.drop_index("ix_usage_daily_api_key_period", table_name="usage_daily")
    op.drop_index("ix_usage_daily_user_period", table_name="usage_daily")
    op.drop_table("usage_daily")
