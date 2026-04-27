"""
Per-job ephemeral workspace management.

Best-effort defense-in-depth; primary control is Codex --sandbox
(Landlock/Seatbelt). ``validate_path_inside`` guards against application
logic bugs, not malicious post-resolution symlink swaps (TOCTOU acknowledged).

Public API:
    make_workspace(job_id)          → Path   — creates /workspaces/<job_id>
    cleanup_workspace(path)         → None   — rmtree; logs on failure
    validate_path_inside(ws, tgt)   → Path   — realpath + commonpath guard
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import UUID

import structlog

from src.codex.exceptions import CodexRunnerError, WorkspaceTraversalError
from src.settings import get_settings

logger = structlog.get_logger(__name__)


def make_workspace(job_id: str | UUID) -> Path:
    """Create an isolated workspace directory for a single job.

    Args:
        job_id: Unique identifier; used as the directory name.

    Returns:
        Absolute Path to the newly created workspace.

    Raises:
        CodexRunnerError: If ``WORKSPACE_ROOT`` does not exist.
        FileExistsError:  If a workspace for ``job_id`` already exists.
    """
    root = Path(get_settings().workspace_root)
    if not root.is_dir():
        raise CodexRunnerError(f"WORKSPACE_ROOT {root} does not exist or is not a directory")
    workspace = root / str(job_id)
    # exist_ok=False → raises FileExistsError on collision (caller's problem)
    workspace.mkdir(mode=0o700, parents=False, exist_ok=False)
    logger.debug("workspace.created", path=str(workspace))
    return workspace


def cleanup_workspace(path: Path) -> None:
    """Recursively delete a workspace directory.

    Never raises — logs WARNING on failure so the caller (which has already
    finished its job) is not disrupted. Idempotent: missing dir is a no-op.
    Does not delete anything outside WORKSPACE_ROOT (belt-and-braces).

    Args:
        path: Absolute path to the workspace to remove.
    """
    root = Path(get_settings().workspace_root).resolve()
    resolved = path.resolve()

    # Safety guard: never escape WORKSPACE_ROOT
    root_str = str(root)
    if not str(resolved).startswith(root_str + os.sep) and resolved != root:
        logger.warning("workspace.cleanup.outside_root", path=str(resolved), root=root_str)
        return

    if not resolved.exists():
        return  # idempotent

    try:
        shutil.rmtree(resolved, ignore_errors=False)
        logger.debug("workspace.removed", path=str(resolved))
    except Exception:
        logger.warning("workspace.cleanup.failed", path=str(resolved), exc_info=True)


def validate_path_inside(workspace: Path, target: Path) -> Path:
    """Verify ``target`` resolves inside ``workspace`` after symlink resolution.

    Uses ``os.path.realpath`` (single resolution) + ``os.path.commonpath``
    comparison. Cannot raise ``ValueError`` from ``relative_to``; only raises
    ``WorkspaceTraversalError`` on bad input (C6 fix).

    Best-effort defense-in-depth — Codex ``--sandbox workspace-write``
    enforcing Landlock (Linux 5.13+) / Seatbelt (macOS) is the primary
    control. This guard protects against application logic bugs, not TOCTOU.

    Args:
        workspace: Absolute path to the job workspace directory.
        target:    Path to validate (may be relative or contain symlinks).

    Returns:
        Resolved absolute Path of ``target``.

    Raises:
        WorkspaceTraversalError: If ``target`` resolves outside ``workspace``.
    """
    ws_real = os.path.realpath(workspace)

    try:
        tgt_real = os.path.realpath(target)
    except OSError as exc:
        raise WorkspaceTraversalError(f"unresolvable target: {exc}") from exc

    try:
        common = os.path.commonpath([tgt_real, ws_real])
    except ValueError as exc:
        # Different drives on Windows or other incomparable path roots.
        raise WorkspaceTraversalError(f"incomparable paths: {exc}") from exc

    if common != ws_real:
        raise WorkspaceTraversalError(f"target {tgt_real!r} is not inside workspace {ws_real!r}")

    return Path(tgt_real)
