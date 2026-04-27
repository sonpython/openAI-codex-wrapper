"""
Alembic migration environment.

Imports Base.metadata so autogenerate detects model changes.
Uses a synchronous URL derived from settings.DATABASE_URL (strips +asyncpg).
"""

from __future__ import annotations

import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import Base so all models are registered in metadata.
# Phase 01: User + ApiKey models are now defined in src.db.models.
from src.db.models import ApiKey, Base, User  # noqa: F401
from src.settings import get_settings

# Alembic Config object — provides access to alembic.ini values.
config = context.config

# Wire up stdlib logging from alembic.ini [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url(async_url: str) -> str:
    """Convert asyncpg URL to a psycopg3 sync URL for Alembic.

    Alembic's synchronous engine requires a sync driver.  We swap ``+asyncpg``
    for ``+psycopg`` (psycopg3/psycopg[binary] declared in pyproject.toml).
    Using ``+psycopg`` explicitly avoids falling back to the default dialect
    resolver which would attempt psycopg2 (not installed).
    """
    return re.sub(r"\+asyncpg", "+psycopg", async_url)


def _get_url() -> str:
    # Allow alembic.ini ``sqlalchemy.url`` override (useful for CI).
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    return _sync_url(get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL script)."""
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _get_url()

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
