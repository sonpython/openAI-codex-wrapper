"""audit_log table

Revision ID: 0006
Revises: 0004
Create Date: 2026-04-27 08:00:00.000000+00:00

Creates the audit_log table for per-request audit trail.
All /v1/* and /admin/* operations write one row via fire-and-forget.
Rows older than audit_log_retention_days (default 90) are purged by daily cron.

Indexes:
  ix_audit_log_created_at        — retention purge + chronological queries
  ix_audit_log_request_id        — correlation lookup
  ix_audit_log_api_key_id        — per-key audit history
  ix_audit_log_user_id           — per-user audit history
  ix_audit_log_api_key_created   — composite tail-by-key queries
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("route", sa.String(length=200), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("codex_cmd", sa.JSON(), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("codex_exit_code", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=120), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_request_id", "audit_log", ["request_id"])
    op.create_index("ix_audit_log_api_key_id", "audit_log", ["api_key_id"])
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])
    op.create_index(
        "ix_audit_log_api_key_created",
        "audit_log",
        ["api_key_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_api_key_created", table_name="audit_log")
    op.drop_index("ix_audit_log_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_api_key_id", table_name="audit_log")
    op.drop_index("ix_audit_log_request_id", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
