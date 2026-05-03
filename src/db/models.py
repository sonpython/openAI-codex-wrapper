"""
SQLAlchemy ORM models.

Phase 00 defined the Base. Phase 01 adds User + ApiKey. Phase 05 adds Job.
Phase 06 adds Plan (tier limits) and UsageCounter (monthly quota tracking).
Phase 08 adds AuditLog (imported from models_audit_log to keep this file <200 LOC).
All models use SQLAlchemy 2.0 Mapped[T] typed columns.

Tables:
  users         — tenant identities; identified by email.
  api_keys      — bearer tokens issued per user; stored as argon2id hashes.
                  Plaintext is shown exactly once at creation (POST /admin/api-keys)
                  and never stored.
  jobs          — async codex execution jobs; status lifecycle: queued → running →
                  succeeded | failed | cancelled.
  plans         — tier rate-limit definitions (free/pro/enterprise). Seeded via migration.
  usage_counter — per-user monthly token consumption; drives monthly quota enforcement.
  audit_log     — per-request audit trail; fire-and-forget writes; 90-day retention.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Date, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Project-wide declarative base.

    All ORM models must inherit from this class so Alembic can discover them
    via ``Base.metadata`` in ``src/db/migrations/env.py``.
    """


class User(Base):
    """A tenant identity. One user may have many api_keys."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    api_keys: Mapped[list[ApiKey]] = relationship("ApiKey", back_populates="user", lazy="noload")

    def __repr__(self) -> str:
        return f"User(id={self.id!s}, email={self.email!r})"


class ApiKey(Base):
    """Hashed bearer token. prefix (first 12 chars) is indexed for fast lookup.

    Key lifecycle:
      - Created via POST /admin/api-keys — plaintext returned once, then lost.
      - Active while revoked_at IS NULL.
      - Soft-deleted: revoked_at set to now(); row retained for audit history.
      - last_used_at updated via fire-and-forget background write (best-effort).

    Tier values: free | pro | ent. Controls rate limits (phase 06).
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # ondelete="RESTRICT" preserves audit trail: deleting a user with active keys
    # requires explicit revoke first. Without this, a future accidental ON DELETE
    # CASCADE migration would silently destroy audit history.
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # First 12 chars of plaintext key — indexed for cheap O(1) lookup before
    # doing the expensive argon2 verify. Prefix space is ~2^72; collisions
    # possible but tolerated (loop verifies all candidates).
    prefix: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    tier: Mapped[str] = mapped_column(String(8), nullable=False, default="free")
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    user: Mapped[User] = relationship("User", back_populates="api_keys", lazy="noload")

    def __repr__(self) -> str:
        return f"ApiKey(id={self.id!s}, prefix={self.prefix!r}, tier={self.tier!r})"


class Job(Base):
    """Long-running async codex execution job.

    Lifecycle: queued → running → succeeded | failed | cancelled.
    Worker process transitions state via crud/jobs.py helpers.
    Diff blob capped at 16MB in DB; API responses truncate to 1MB with flag.
    """

    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # api_key_id links job to the key used at submission time.
    # SET NULL on delete: revoking a key preserves job history.
    api_key_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Status values: queued | running | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(String(200), nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    # Mode values: read-only | workspace-write
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full diff stored (up to 16MB); API responses truncate to 1MB.
    diff_blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff_size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    files_changed: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    stderr_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Per-job token counts recorded at completion time by the worker.
    # Summed in admin usage queries for daily token breakdowns.
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    def __repr__(self) -> str:
        return f"Job(id={self.id!s}, status={self.status!r}, user_id={self.user_id!s})"


class Plan(Base):
    """Rate-limit tier definitions.

    Seeded via migration 0004_plans.py.  Changes are rare; middleware caches
    values in-process with a 5-min TTL (phase-06 tier_cache).

    Columns:
      tier           — primary key string: free | pro | enterprise
      rpm            — requests per minute limit (per API key)
      tpm            — tokens per minute limit (per API key)
      concurrent     — max in-flight requests per API key
      monthly_tokens — monthly token quota per user
    """

    __tablename__ = "plans"

    tier: Mapped[str] = mapped_column(String(20), primary_key=True)
    rpm: Mapped[int] = mapped_column(Integer, nullable=False)
    tpm: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrent: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"Plan(tier={self.tier!r}, rpm={self.rpm}, tpm={self.tpm})"


class UsageCounter(Base):
    """Per-user monthly token consumption.

    Composite PK (user_id, period) where period = first day of the UTC month.
    Written via INSERT … ON CONFLICT DO UPDATE (upsert) in crud/usage_counter.py.
    Hot-path reads are served from Redis cache (TTL 60s).
    """

    __tablename__ = "usage_counter"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    period: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    def __repr__(self) -> str:
        return f"UsageCounter(user_id={self.user_id!s}, period={self.period!s})"


# Phase 08: AuditLog is defined in a separate module to keep this file <200 LOC.
# Import here so Alembic discovers it via Base.metadata when it imports src.db.models.
from src.db.models_audit_log import AuditLog  # noqa: E402, F401

# Phase 09: UsageDaily is defined in a separate module to keep this file <200 LOC.
# Import here so Alembic discovers it via Base.metadata when it imports src.db.models.
from src.db.models_usage_daily import UsageDaily  # noqa: E402, F401

# Explicit re-exports so `from src.db.models import UsageDaily` type-checks
# under mypy's --strict (PEP 484 implicit re-export rules).
__all__ = ["AuditLog", "UsageDaily"]
