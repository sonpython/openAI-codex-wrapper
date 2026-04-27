"""
Phase-08 audit log integration smoke test.

Verifies that audit_log.emit() schedules a _persist() call that would
INSERT a row with the correct fields — using a mocked bg_session so no
real DB is required.

Covers:
  - emit with admin=True, action="rotate" → fields passed to _persist
  - emit with prompt → prompt_hash set, raw prompt absent
  - emit with no prompt → prompt_hash absent
  - Background task completes (no GC warning)
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _clear_bg_tasks():
    import src.db.crud.audit_log as m

    m._BG_TASKS.clear()
    yield
    m._BG_TASKS.clear()


@pytest.mark.asyncio
async def test_emit_admin_rotate_fields():
    """Admin rotate audit entry → correct fields passed to session.add."""
    import src.db.crud.audit_log as m

    key_id = uuid4()
    inserted_rows: list = []

    async def fake_persist(fields):
        inserted_rows.append(fields)

    mock_settings = MagicMock()
    mock_settings.audit_log_prompt = False

    with (
        patch.object(m, "_persist", side_effect=fake_persist),
        patch.object(m, "get_settings", return_value=mock_settings),
    ):
        m.emit(
            admin=True,
            action="rotate",
            target_id=key_id,
            route="/admin/api-keys/{id}/rotate",
            method="POST",
            status_code=200,
        )
        await asyncio.sleep(0.05)

    assert len(inserted_rows) == 1
    row = inserted_rows[0]
    assert row["admin"] is True
    assert row["action"] == "rotate"
    assert str(row["target_id"]) == str(key_id)
    assert row["status_code"] == 200


@pytest.mark.asyncio
async def test_emit_prompt_hash_not_raw():
    """emit with prompt → only sha256 hash stored, never raw text."""
    import src.db.crud.audit_log as m

    inserted: list = []

    async def fake_persist(fields):
        inserted.append(fields)

    mock_settings = MagicMock()
    mock_settings.audit_log_prompt = False

    with (
        patch.object(m, "_persist", side_effect=fake_persist),
        patch.object(m, "get_settings", return_value=mock_settings),
    ):
        m.emit(prompt="this is my secret prompt", route="/v1/chat/completions")
        await asyncio.sleep(0.05)

    assert len(inserted) == 1
    row = inserted[0]
    expected = hashlib.sha256(b"this is my secret prompt").hexdigest()
    assert row.get("prompt_hash") == expected
    # Raw prompt must not appear anywhere in the fields
    for v in row.values():
        if isinstance(v, str):
            assert "secret prompt" not in v


@pytest.mark.asyncio
async def test_emit_no_prompt_no_hash():
    """emit without prompt → prompt_hash key absent from fields."""
    import src.db.crud.audit_log as m

    inserted: list = []

    async def fake_persist(fields):
        inserted.append(fields)

    mock_settings = MagicMock()
    mock_settings.audit_log_prompt = False

    with (
        patch.object(m, "_persist", side_effect=fake_persist),
        patch.object(m, "get_settings", return_value=mock_settings),
    ):
        m.emit(route="/v1/models", method="GET", status_code=200)
        await asyncio.sleep(0.05)

    assert len(inserted) == 1
    assert "prompt_hash" not in inserted[0]


@pytest.mark.asyncio
async def test_bg_tasks_cleared_after_completion():
    """_BG_TASKS set shrinks to 0 after tasks complete (no GC warning)."""
    import src.db.crud.audit_log as m

    mock_settings = MagicMock()
    mock_settings.audit_log_prompt = False

    with (
        patch("src.db.crud.audit_log.bg_session") as mock_bg,
        patch.object(m, "get_settings", return_value=mock_settings),
    ):
        mock_sess = AsyncMock()
        mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
        mock_sess.__aexit__ = AsyncMock(return_value=False)
        mock_sess.add = MagicMock()
        mock_sess.commit = AsyncMock()
        mock_bg.return_value = mock_sess

        m.emit(route="/v1/chat/completions", status_code=200)
        assert len(m._BG_TASKS) == 1
        await asyncio.sleep(0.1)

    assert len(m._BG_TASKS) == 0
