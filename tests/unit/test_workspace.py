"""
Unit tests for src/codex/workspace.py.

Covers:
- make_workspace: happy path, missing root, duplicate id
- cleanup_workspace: happy path, missing dir (idempotent), outside-root guard
- validate_path_inside: valid nested, absolute outside, relative-with-..,
  symlink pointing outside workspace
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from src.codex.exceptions import WorkspaceTraversalError
from src.codex.workspace import cleanup_workspace, make_workspace, validate_path_inside

# ── make_workspace ────────────────────────────────────────────────────────────


def test_make_workspace_creates_dir_with_correct_mode(tmp_path: Path) -> None:
    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(tmp_path)
        ws = make_workspace("job-001")

    assert ws.exists()
    assert ws.is_dir()
    assert ws.name == "job-001"
    # mode 0o700: owner rwx, no group/other
    mode = oct(ws.stat().st_mode)[-3:]
    assert mode == "700"


def test_make_workspace_raises_if_root_missing(tmp_path: Path) -> None:
    from src.codex.exceptions import CodexRunnerError

    missing = tmp_path / "nonexistent"
    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(missing)
        with pytest.raises(CodexRunnerError, match="WORKSPACE_ROOT"):
            make_workspace("job-001")


def test_make_workspace_raises_on_duplicate_id(tmp_path: Path) -> None:
    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(tmp_path)
        make_workspace("job-dup")
        with pytest.raises(FileExistsError):
            make_workspace("job-dup")


# ── cleanup_workspace ─────────────────────────────────────────────────────────


def test_cleanup_workspace_removes_directory(tmp_path: Path) -> None:
    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(tmp_path)
        ws = make_workspace("job-clean")

    (ws / "file.txt").write_text("hello")
    assert ws.exists()

    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(tmp_path)
        cleanup_workspace(ws)

    assert not ws.exists()


def test_cleanup_workspace_idempotent_if_missing(tmp_path: Path) -> None:
    missing = tmp_path / "already-gone"
    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(tmp_path)
        # Should not raise
        cleanup_workspace(missing)


def test_cleanup_workspace_refuses_path_outside_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)

    with patch("src.codex.workspace.get_settings") as mock_settings:
        mock_settings.return_value.workspace_root = str(tmp_path)
        # Should log warning + return; directory should NOT be deleted
        cleanup_workspace(outside)

    assert outside.exists()


# ── validate_path_inside ──────────────────────────────────────────────────────


def test_validate_accepts_direct_child(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    child = ws / "output.txt"
    child.touch()

    result = validate_path_inside(ws, child)
    assert result == child.resolve()


def test_validate_accepts_nested_path(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    nested = ws / "a" / "b" / "c.py"
    nested.parent.mkdir(parents=True)
    nested.touch()

    result = validate_path_inside(ws, nested)
    assert str(result).startswith(str(ws.resolve()))


def test_validate_rejects_absolute_outside(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = Path("/etc/passwd")

    with pytest.raises(WorkspaceTraversalError):
        validate_path_inside(ws, outside)


def test_validate_rejects_relative_dotdot_escape(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    # Relative path that resolves outside workspace
    escape = ws / ".." / ".." / "etc" / "passwd"

    with pytest.raises(WorkspaceTraversalError):
        validate_path_inside(ws, escape)


def test_validate_rejects_symlink_pointing_outside(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside_target = tmp_path / "secret.txt"
    outside_target.write_text("secret")

    symlink = ws / "evil_link"
    symlink.symlink_to(outside_target)

    with pytest.raises(WorkspaceTraversalError):
        validate_path_inside(ws, symlink)


def test_validate_workspace_itself_is_accepted(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()

    result = validate_path_inside(ws, ws)
    assert result == ws.resolve()


def test_validate_raises_traversal_error_not_value_error(tmp_path: Path) -> None:
    """Regression: must never leak ValueError to callers (C6 fix)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    escape = ws / "../../../outside"

    with pytest.raises(WorkspaceTraversalError):
        validate_path_inside(ws, escape)
