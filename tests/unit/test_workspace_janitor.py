"""
Tests for the workspace janitor cron task.

Covers:
  - Stale dir (no active job, mtime > 1h) → removed
  - Fresh dir (mtime < 1h) → preserved
  - Active job dir → preserved
  - Missing workspace root → returns cleaned=0 gracefully
  - DB fetch failure → returns cleaned=0 gracefully
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.workers.janitor import _STALE_AGE_SECONDS, cleanup_stale_workspaces


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


def _make_dir(root: Path, name: str, age_seconds: int) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    # Set mtime to `age_seconds` ago
    old_time = time.time() - age_seconds
    os.utime(d, (old_time, old_time))
    return d


@pytest.mark.asyncio
async def test_stale_dir_removed(workspace_root, monkeypatch):
    workspace_root.mkdir()
    stale = _make_dir(workspace_root, "stale-job-id", _STALE_AGE_SECONDS + 100)

    monkeypatch.setattr(
        "src.workers.janitor.get_settings", lambda: MagicMock(workspace_root=str(workspace_root))
    )

    with (
        patch("src.workers.janitor.bg_session") as mock_bg,
        patch(
            "src.workers.janitor.jobs_crud.list_active_job_ids", new_callable=AsyncMock
        ) as mock_active,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_bg.return_value = mock_session
        mock_active.return_value = set()

        result = await cleanup_stale_workspaces({})

    assert result["cleaned"] == 1
    assert not stale.exists()


@pytest.mark.asyncio
async def test_fresh_dir_preserved(workspace_root, monkeypatch):
    workspace_root.mkdir()
    fresh = _make_dir(workspace_root, "fresh-job-id", 10)  # 10 seconds old

    monkeypatch.setattr(
        "src.workers.janitor.get_settings", lambda: MagicMock(workspace_root=str(workspace_root))
    )

    with (
        patch("src.workers.janitor.bg_session") as mock_bg,
        patch(
            "src.workers.janitor.jobs_crud.list_active_job_ids", new_callable=AsyncMock
        ) as mock_active,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_bg.return_value = mock_session
        mock_active.return_value = set()

        result = await cleanup_stale_workspaces({})

    assert result["cleaned"] == 0
    assert fresh.exists()


@pytest.mark.asyncio
async def test_active_job_dir_preserved(workspace_root, monkeypatch):
    workspace_root.mkdir()
    active_id = "active-job-uuid"
    active_dir = _make_dir(workspace_root, active_id, _STALE_AGE_SECONDS + 100)

    monkeypatch.setattr(
        "src.workers.janitor.get_settings", lambda: MagicMock(workspace_root=str(workspace_root))
    )

    with (
        patch("src.workers.janitor.bg_session") as mock_bg,
        patch(
            "src.workers.janitor.jobs_crud.list_active_job_ids", new_callable=AsyncMock
        ) as mock_active,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_bg.return_value = mock_session
        mock_active.return_value = {active_id}

        result = await cleanup_stale_workspaces({})

    assert result["cleaned"] == 0
    assert active_dir.exists()


@pytest.mark.asyncio
async def test_missing_workspace_root(tmp_path, monkeypatch):
    missing = tmp_path / "nonexistent"
    monkeypatch.setattr(
        "src.workers.janitor.get_settings", lambda: MagicMock(workspace_root=str(missing))
    )
    result = await cleanup_stale_workspaces({})
    assert result == {"cleaned": 0}


@pytest.mark.asyncio
async def test_db_failure_returns_zero(workspace_root, monkeypatch):
    workspace_root.mkdir()
    _make_dir(workspace_root, "some-job", _STALE_AGE_SECONDS + 100)

    monkeypatch.setattr(
        "src.workers.janitor.get_settings", lambda: MagicMock(workspace_root=str(workspace_root))
    )

    with patch("src.workers.janitor.bg_session") as mock_bg:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("DB down"))
        mock_bg.return_value = mock_session

        result = await cleanup_stale_workspaces({})

    assert result == {"cleaned": 0}
