"""
AuditLog ORM model — phase 08.

Kept in a separate module so models.py stays under 200 LOC.
models.py imports this at its bottom (after Base is defined), so
importing Base from models here is safe — no circular-import issue.
Alembic discovers audit_log via Base.metadata because env.py imports
src.db.models, which triggers this module.

Columns (per phase-08 spec):
  id             bigserial PK
  created_at     timestamptz indexed
  request_id     text indexed
  api_key_id     uuid nullable indexed
  user_id        uuid nullable indexed
  admin          bool
  route          text
  method         text
  status_code    int
  duration_ms    int
  codex_cmd      text[]
  prompt_hash    text (sha256; never raw unless AUDIT_LOG_PROMPT=true)
  input_tokens   int
  output_tokens  int
  codex_exit_code int
  error_class    text
  target_id      uuid (admin ops)
  action         text (create|rotate|revoke|stderr_retrieve)
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, BigInteger, Boolean, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

# Base is defined in models.py. This module is only ever imported from the
# bottom of models.py (after Base exists), so this import is safe.
from src.db.models import Base


class AuditLog(Base):
    """Per-request audit trail row.

    Written asynchronously via fire-and-forget (audit_log.emit) so the
    request path is never blocked. Rows older than audit_log_retention_days
    are purged by the daily cron task (janitor.purge_audit_log).
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        # Composite index for tail-by-key queries
        Index("ix_audit_log_api_key_created", "api_key_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # No FK constraints — allows admin rows (api_key_id=NULL, user_id=NULL)
    # and avoids cross-module FK dependency in test databases.
    api_key_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    user_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    route: Mapped[str | None] = mapped_column(String(200), nullable=True)
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)
    # JSON works in both Postgres and SQLite (unit tests). ARRAY(Text) would be
    # ideal on Postgres but breaks SQLite schema creation used in unit tests.
    codex_cmd: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    codex_exit_code: Mapped[int | None] = mapped_column(nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    action: Mapped[str | None] = mapped_column(String(40), nullable=True)

    def __repr__(self) -> str:
        return (
            f"AuditLog(id={self.id}, route={self.route!r}, "
            f"status={self.status_code}, admin={self.admin})"
        )
