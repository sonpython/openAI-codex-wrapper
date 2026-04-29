"""
Async git diff capture helper.

Captures diff blob + changed file list after codex execution.

Diff blob is capped at 16MB before DB insert; the ``diff_truncated`` flag
signals callers that the stored blob is incomplete. The full diff lives on
the worker filesystem until workspace cleanup — phase 08 adds S3 offload.

Public API:
    capture_diff(repo_dir, head_before) -> DiffResult
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# 16 MB cap for DB storage of diff_blob.
DIFF_DB_MAX_BYTES = 16 * 1_048_576


@dataclass
class DiffResult:
    diff_blob: str | None
    diff_size_bytes: int
    diff_truncated: bool
    files_changed: list[str]


async def _run_git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run a git command, return (returncode, stdout_decoded)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, _ = await proc.communicate()
    return proc.returncode or 0, stdout_bytes.decode("utf-8", errors="replace")


async def capture_diff(
    repo_dir: Path | str,
    head_before: str,
) -> DiffResult:
    """Capture git diff between head_before and current HEAD.

    Also collects untracked files via ``git ls-files --others``.

    Args:
        repo_dir:    Path to the cloned repository.
        head_before: SHA recorded before codex ran (from git_rev_parse_head).

    Returns:
        DiffResult with blob, size, truncation flag, and files list.
    """
    cwd = str(repo_dir)

    # ── Diff blob (tracked changes) ───────────────────────────────────────
    _, diff_text = await _run_git(
        ["-C", cwd, "diff", head_before, "HEAD", "--"],
        cwd=cwd,
    )

    raw_bytes = diff_text.encode("utf-8", errors="replace")
    diff_size_bytes = len(raw_bytes)
    diff_truncated = False

    if diff_size_bytes > DIFF_DB_MAX_BYTES:
        diff_truncated = True
        raw_bytes = raw_bytes[:DIFF_DB_MAX_BYTES]
        diff_text = raw_bytes.decode("utf-8", errors="ignore")
        logger.warning(
            "git_diff.truncated",
            original_bytes=diff_size_bytes,
            cap_bytes=DIFF_DB_MAX_BYTES,
        )

    blob: str | None = diff_text if diff_text else None

    # ── Changed tracked files ──────────────────────────────────────────────
    _, names_text = await _run_git(
        ["-C", cwd, "diff", "--name-only", head_before, "HEAD"],
        cwd=cwd,
    )
    tracked_files = [f for f in names_text.splitlines() if f.strip()]

    # ── Untracked files (new files codex created) ──────────────────────────
    _, untracked_text = await _run_git(
        ["-C", cwd, "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
    )
    untracked_files = [f for f in untracked_text.splitlines() if f.strip()]

    files_changed = tracked_files + untracked_files

    logger.debug(
        "git_diff.captured",
        diff_size_bytes=diff_size_bytes,
        diff_truncated=diff_truncated,
        files_changed_count=len(files_changed),
    )

    return DiffResult(
        diff_blob=blob,
        diff_size_bytes=diff_size_bytes,
        diff_truncated=diff_truncated,
        files_changed=files_changed,
    )
