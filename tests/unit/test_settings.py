"""
Unit tests for src/settings.py.

Tests that:
- Settings() succeeds when all required vars are provided.
- Settings() raises ValidationError when a required var (DATABASE_URL) is absent.
- Default values are applied for optional fields.
- Pool sizing defaults match the spec (capacity math in engine.py).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError


def test_settings_loads_with_required_vars() -> None:
    """Settings succeeds when DATABASE_URL and REDIS_URL are provided."""
    from src.settings import Settings

    s = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
    )
    assert s.wrapper_env == "dev"
    assert s.database_url == "postgresql+asyncpg://u:p@localhost:5432/db"
    assert s.redis_url == "redis://localhost:6379/0"


def test_settings_raises_on_missing_database_url() -> None:
    """Settings() raises ValidationError when DATABASE_URL is not set.

    Must temporarily unset DATABASE_URL from the environment because conftest.py
    sets it as a default so other tests don't fail. pydantic-settings reads from
    env when no constructor arg is provided, so we must clear it here.
    """
    from src.settings import Settings

    env_without_db = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    with (
        patch.dict(os.environ, env_without_db, clear=True),
        pytest.raises(ValidationError) as exc_info,
    ):
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors = exc_info.value.errors()
    field_names = {e["loc"][0] for e in errors}
    assert "database_url" in field_names


def test_settings_raises_on_missing_redis_url() -> None:
    """Settings() raises ValidationError when REDIS_URL is not set.

    See test_settings_raises_on_missing_database_url for env-isolation rationale.
    """
    from src.settings import Settings

    env_without_redis = {k: v for k, v in os.environ.items() if k != "REDIS_URL"}
    with (
        patch.dict(os.environ, env_without_redis, clear=True),
        pytest.raises(ValidationError) as exc_info,
    ):
        Settings(_env_file=None)  # type: ignore[call-arg]

    errors = exc_info.value.errors()
    field_names = {e["loc"][0] for e in errors}
    assert "redis_url" in field_names


def test_settings_pool_defaults() -> None:
    """Default pool sizes match the spec capacity math documented in engine.py."""
    from src.settings import Settings

    s = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
    )
    assert s.db_pool_size == 20
    assert s.db_max_overflow == 10
    assert s.db_pool_timeout == 2.0
    assert s.bg_db_pool_size == 3
    assert s.bg_db_pool_timeout == 0.5


def test_settings_job_defaults() -> None:
    """Default job lifecycle values match spec."""
    from src.settings import Settings

    s = Settings(
        database_url="postgresql+asyncpg://u:p@localhost:5432/db",
        redis_url="redis://localhost:6379/0",
    )
    assert s.job_timeout_seconds == 900
    assert s.job_cancel_grace_seconds == 5
    assert s.codex_bin == "codex"
    assert s.otel_exporter_otlp_endpoint is None
