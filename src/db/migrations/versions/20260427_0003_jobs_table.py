"""jobs table

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-27 02:03:00.000000+00:00

Creates the jobs table for async codex execution tracking.

Indexes:
  - ix_jobs_user_id   — per-user job listing queries
  - ix_jobs_status    — worker orphan recovery (status='running')
  - ix_jobs_user_enqueued — composite for GET /v1/codex/jobs list (future)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("branch", sa.String(length=200), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("diff_blob", sa.Text(), nullable=True),
        sa.Column("diff_size_bytes", sa.Integer(), nullable=True),
        # JSONB on Postgres; falls back to JSON on SQLite in unit tests.
        sa.Column("files_changed", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stderr_tail", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"], unique=False)
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)
    # Composite index for future list endpoint: per-user listing ordered by time.
    op.create_index(
        "ix_jobs_user_enqueued",
        "jobs",
        ["user_id", "enqueued_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_user_enqueued", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
