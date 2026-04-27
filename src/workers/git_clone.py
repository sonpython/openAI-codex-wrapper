"""
Async subprocess git clone helper with timeout enforcement.

URL is re-validated immediately before exec (defense-in-depth against TOCTOU
on the DB row). ``git clone --depth 1`` avoids fetching full history.

Public API:
    git_clone(repo_url, branch, target_dir, timeout) -> tuple[bool, str]
    git_rev_parse_head(repo_dir) -> str
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import structlog

from src.observability.tracing import start_span

logger = structlog.get_logger(__name__)

# Same regex as JobCreateRequest validator — re-checked at exec time.
_GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:\.git)?/?$")

_STDERR_CAP = 4096  # 4KB cap on returned stderr tail


class GitCloneError(Exception):
    """Raised when git clone fails or times out."""


async def git_clone(
    repo_url: str,
    branch: str,
    target_dir: Path | str,
    timeout: float = 60.0,
) -> tuple[bool, str]:
    """Clone a public GitHub repo shallowly into target_dir.

    Args:
        repo_url:   Public GitHub HTTPS URL (re-validated before exec).
        branch:     Branch name to clone.
        target_dir: Destination directory (must not already exist).
        timeout:    Subprocess timeout in seconds.

    Returns:
        Tuple (success: bool, stderr_tail: str).
        On success stderr_tail may be non-empty (git status lines are fine).

    Raises:
        GitCloneError: On timeout or non-zero exit; includes stderr tail.
    """
    # Defense-in-depth: re-validate URL right before handing to subprocess.
    if not _GITHUB_URL_RE.match(repo_url):
        raise GitCloneError(f"Refusing to clone non-GitHub URL: {repo_url!r}")

    target = str(target_dir)
    cmd = ["git", "clone", "--depth", "1", "-b", branch, repo_url, target]
    log = logger.bind(repo_url=repo_url, branch=branch, target=target)
    log.debug("git_clone.starting", cmd=cmd)

    async with start_span("git.clone", {"repo_url": repo_url, "branch": branch}):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                import contextlib  # noqa: PLC0415

                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                # Drain to avoid ResourceWarning on unclosed streams.
                with contextlib.suppress(Exception):
                    await proc.communicate()
                raise GitCloneError(
                    f"git clone timed out after {timeout}s for {repo_url!r}"
                ) from None

        except GitCloneError:
            raise
        except Exception as exc:
            raise GitCloneError(f"git clone subprocess error: {exc}") from exc

        stderr_tail = stderr_bytes[-_STDERR_CAP:].decode("utf-8", errors="replace")
        rc = proc.returncode

        if rc != 0:
            log.warning("git_clone.failed", exit_code=rc, stderr_tail=stderr_tail)
            raise GitCloneError(f"git clone exited {rc} for {repo_url!r}: {stderr_tail[:200]}")

        log.debug("git_clone.succeeded")
        return True, stderr_tail


async def git_rev_parse_head(repo_dir: Path | str) -> str:
    """Return the current HEAD commit SHA in repo_dir.

    Raises:
        GitCloneError: If rev-parse fails.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_dir),
        "rev-parse",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raise GitCloneError(f"git rev-parse HEAD failed: {stderr[:200]}")
    return stdout_bytes.decode().strip()
