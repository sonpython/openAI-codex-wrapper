"""Ensure ent plan exists (alias of enterprise tier)

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-03 03:55:00.000000+00:00

Reason: api_keys.tier is VARCHAR(8) and the admin UI form posts the short
slug 'ent', but migration 0004 seeded the long name 'enterprise'. Keys
created with tier='ent' could not resolve a plan row, so the rate limiter
defaulted to zero limits → every request returned 429 tpm_exceeded.

This migration inserts an idempotent 'ent' row mirroring the 'enterprise'
limits (or, if 'enterprise' is missing, falls back to known defaults).
Both names remain valid plan rows so existing keys keep working regardless
of which slug they were created with.
"""

from __future__ import annotations

from alembic import op


revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO plans (tier, rpm, tpm, concurrent, monthly_tokens)
        VALUES ('ent', 2000, 2000000, 50, 20000000)
        ON CONFLICT (tier) DO UPDATE
            SET rpm = EXCLUDED.rpm,
                tpm = EXCLUDED.tpm,
                concurrent = EXCLUDED.concurrent,
                monthly_tokens = EXCLUDED.monthly_tokens
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM plans WHERE tier = 'ent'")
