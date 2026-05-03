"""
SQLAlchemy declarative base — shared across all ORM model modules.

Kept in its own module so satellite model files (models_audit_log.py,
models_usage_daily.py) can import Base without creating a circular import
with models.py.

Import chain that caused the circular import before this fix:
  models.py → models_audit_log.py → models.py  (cycle!)

With this module:
  models.py           → src.db.base  (no cycle)
  models_audit_log.py → src.db.base  (no cycle)
  models_usage_daily.py → src.db.base (no cycle)
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base.

    All ORM models must inherit from this class so Alembic can discover them
    via ``Base.metadata`` in ``src/db/migrations/env.py``.
    """
