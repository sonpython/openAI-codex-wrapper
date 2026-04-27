"""users and api_keys

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-27 00:01:00.000000+00:00

Creates two tables:
  users    — tenant identities; email is unique.
  api_keys — hashed bearer tokens; prefix indexed for fast lookup.

Constraints:
  - users.email          UNIQUE
  - api_keys.key_hash    UNIQUE
  - api_keys.prefix      INDEX (non-unique; for cheap prefix-first lookup)
  - api_keys.user_id     FK → users.id
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgcrypto provides gen_random_uuid() for server-side UUID generation.
    # Required for raw SQL INSERT (e.g. restore scripts) that omit the id column.
    # SQLite (used in unit tests) ignores CREATE EXTENSION silently via the
    # client-side uuid4 default on the ORM model — no test breakage.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "tier",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'free'"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("users")
