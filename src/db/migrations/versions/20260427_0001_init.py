"""init

Revision ID: 0001
Revises:
Create Date: 2026-04-27 00:00:00.000000+00:00

Empty initial revision — no tables yet.  Phase 01 adds users + api_keys.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No tables in phase 00 — Base has no mapped models yet.
    pass


def downgrade() -> None:
    pass
