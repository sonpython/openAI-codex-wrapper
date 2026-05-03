"""api_keys mode column

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-04 00:10:00.000000+00:00

Adds api_keys.mode VARCHAR(16) NOT NULL DEFAULT 'sandbox' with a CHECK
constraint limiting values to ('sandbox', 'vps', 'local-bridge').

Existing rows receive DEFAULT 'sandbox' — no data migration needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'sandbox'"),
        ),
    )
    op.create_check_constraint(
        "ck_api_keys_mode",
        "api_keys",
        "mode IN ('sandbox', 'vps', 'local-bridge')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_api_keys_mode", "api_keys", type_="check")
    op.drop_column("api_keys", "mode")
