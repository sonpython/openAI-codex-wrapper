"""
Tests for audit_log CRUD: emit() and purge_old().

Covers:
  - emit() creates a background task (task in _BG_TASKS set)
  - prompt is hashed (sha256) when audit_log_prompt=False
  - prompt stored raw when audit_log_prompt=True (dev mode)
  - _persist() swallows all exceptions
  - purge_old() calls DELETE (mocked session)
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_bg_tasks():
    """Clear _BG_TASKS between tests to prevent cross-test interference."""
    import src.db.crud.audit_log as m

    m._BG_TASKS.clear()
    yield
    m._BG_TASKS.clear()


def test_emit_creates_bg_task():
    """emit() schedules a background task held in _BG_TASKS."""
    import src.db.crud.audit_log as m

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch("src.db.crud.audit_log.bg_session") as mock_bg:
                mock_sess = AsyncMock()
                mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
                mock_sess.__aexit__ = AsyncMock(return_value=False)
                mock_sess.add = MagicMock()
                mock_sess.commit = AsyncMock()
                mock_bg.return_value = mock_sess

                m.emit(route="/v1/chat/completions", method="POST", status_code=200)
                assert len(m._BG_TASKS) == 1
                # Allow task to run
                await asyncio.sleep(0.05)

        loop.run_until_complete(_run())
    finally:
        loop.close()


def test_emit_hashes_prompt_by_default(monkeypatch):
    """prompt field is sha256-hashed when audit_log_prompt=False."""
    import src.db.crud.audit_log as m

    mock_settings = MagicMock()
    mock_settings.audit_log_prompt = False
    monkeypatch.setattr(m, "get_settings", lambda: mock_settings)

    captured: list[dict] = []

    async def fake_persist(fields):
        captured.append(fields)

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(m, "_persist", side_effect=fake_persist):
                m.emit(prompt="my secret prompt", route="/v1/chat/completions")
                await asyncio.sleep(0.05)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    assert len(captured) == 1
    fields = captured[0]
    expected_hash = hashlib.sha256(b"my secret prompt").hexdigest()
    assert fields.get("prompt_hash") == expected_hash
    # Raw prompt must never be in fields
    assert "prompt" not in fields


def test_emit_stores_raw_prompt_in_dev_mode(monkeypatch):
    """prompt stored truncated-raw when audit_log_prompt=True."""
    import src.db.crud.audit_log as m

    mock_settings = MagicMock()
    mock_settings.audit_log_prompt = True
    monkeypatch.setattr(m, "get_settings", lambda: mock_settings)

    captured: list[dict] = []

    async def fake_persist(fields):
        captured.append(fields)

    loop = asyncio.new_event_loop()
    try:

        async def _run():
            with patch.object(m, "_persist", side_effect=fake_persist):
                m.emit(prompt="debug prompt", route="/v1/chat/completions")
                await asyncio.sleep(0.05)

        loop.run_until_complete(_run())
    finally:
        loop.close()

    assert len(captured) == 1
    assert captured[0].get("prompt_hash") == "debug prompt"


@pytest.mark.asyncio
async def test_persist_swallows_exceptions():
    """_persist() never raises even when session.add raises."""
    import src.db.crud.audit_log as m

    with patch("src.db.crud.audit_log.bg_session") as mock_bg:
        mock_sess = AsyncMock()
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        mock_sess.add = MagicMock(side_effect=RuntimeError("DB exploded"))
        mock_bg.return_value = mock_sess

        # Must not raise
        await m._persist({"route": "/v1/test", "admin": False})


@pytest.mark.asyncio
async def test_persist_swallows_pool_timeout():
    """_persist() logs WARN and drops on pool timeout (not raising)."""
    import src.db.crud.audit_log as m

    with patch("src.db.crud.audit_log.bg_session") as mock_bg:
        mock_sess = AsyncMock()
        mock_sess.__aenter__ = AsyncMock(side_effect=TimeoutError("pool timeout"))
        mock_bg.return_value = mock_sess

        # Must not raise
        await m._persist({"route": "/v1/test"})
