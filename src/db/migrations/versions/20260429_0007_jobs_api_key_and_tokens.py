"""jobs: add api_key_id FK + input_tokens + output_tokens columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-29 17:53:00.000000+00:00

Fixes two known gaps from Phase 4:
  1. /admin/usage/by-key/{key_id} returned empty — jobs had no api_key_id FK.
  2. Daily token breakdown returned 0 — jobs had no per-job token columns.

api_key_id uses SET NULL on delete so revoking a key doesn't break job history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "api_key_id",
            sa.Uuid(),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "input_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "output_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index("ix_jobs_api_key_id", "jobs", ["api_key_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_api_key_id", table_name="jobs")
    op.drop_column("jobs", "output_tokens")
    op.drop_column("jobs", "input_tokens")
    op.drop_column("jobs", "api_key_id")
