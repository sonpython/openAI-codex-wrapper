"""
SQLAlchemy ORM models.

Phase 00 defined the Base. Phase 01 adds User + ApiKey.
All models use SQLAlchemy 2.0 Mapped[T] typed columns.

Tables:
  users    — tenant identities; identified by email.
  api_keys — bearer tokens issued per user; stored as argon2id hashes.
             Plaintext is shown exactly once at creation (POST /admin/api-keys)
             and never stored.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String, func
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
