"""plans table + seed + usage_counter table

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-27 03:00:00.000000+00:00

Creates:
  plans         — tier rate-limit definitions (free/pro/enterprise).
  usage_counter — per-user monthly token consumption (monthly quota tracking).

Seed values for plans:
  free:       rpm=20,   tpm=20000,   concurrent=2,  monthly=100000
  pro:        rpm=200,  tpm=200000,  concurrent=10, monthly=2000000
  enterprise: rpm=2000, tpm=2000000, concurrent=50, monthly=20000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=False),
        sa.Column("tpm", sa.Integer(), nullable=False),
        sa.Column("concurrent", sa.Integer(), nullable=False),
        sa.Column("monthly_tokens", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tier"),
    )

    op.execute(
        sa.text(
            "INSERT INTO plans (tier, rpm, tpm, concurrent, monthly_tokens) VALUES "
            "('free', 20, 20000, 2, 100000), "
            "('pro', 200, 200000, 10, 2000000), "
            "('enterprise', 2000, 2000000, 50, 20000000)"
        )
    )

    op.create_table(
        "usage_counter",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column(
            "requests",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "input_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_id", "period"),
    )


def downgrade() -> None:
    op.drop_table("usage_counter")
    op.drop_table("plans")
