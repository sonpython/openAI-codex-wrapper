"""
SSRF-hardened HTTP HEAD check for repo URLs before enqueue.

Validates that a repo URL:
  1. Returns non-3xx (follow_redirects=False — redirect is a SSRF signal)
  2. Does not resolve to any private IPv4/IPv6 address (SSRFGuardedTransport)
  3. Responds within timeout (default 5s, 2 retries)

Cache: positive results cached in Redis for 300s to avoid repeated HEAD calls
for the same URL in burst scenarios.

Cache key: repo_head:{sha256(url)} → "1" EX 300

Called from POST /v1/codex/jobs route before DB insert + Arq enqueue.
On any rejection: raises RepoUrlCheckError with a stable 422 message.
Never leaks resolution details to the caller.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Any

import httpx
import structlog

from src.gateway.ssrf_transport import SSRFBlockedError, make_ssrf_client

logger = structlog.get_logger(__name__)


class RepoUrlCheckError(Exception):
    """Raised when the repo URL fails validation (SSRF, redirect, timeout, 4xx/5xx)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


def _cache_key(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    return f"repo_head:{digest}"


async def check_repo_url(url: str, redis: Any | None = None) -> None:
    """Perform SSRF-guarded HEAD check on the repo URL.

    Args:
        url:   The repo URL to check (already regex-validated by schema).
        redis: Optional Redis client for caching positive results.

    Raises:
        RepoUrlCheckError: if the URL fails any validation.
    """
    from src.settings import get_settings  # noqa: PLC0415

    settings = get_settings()

    # Check Redis cache for a positive result (avoid repeated HEAD calls)
    if redis is not None:
        cache_key = _cache_key(url)
        try:
            cached = await redis.get(cache_key)
            if cached is not None:
                logger.debug("repo_head_check.cache_hit", url=url)
                return
        except Exception:
            logger.debug("repo_head_check.cache_error", url=url, exc_info=True)

    timeout = float(settings.repo_head_timeout)
    last_error: Exception | None = None

    for attempt in range(2):  # 2 retries
        try:
            async with make_ssrf_client(timeout=timeout) as client:
                response = await client.head(url)

            # follow_redirects=False — 3xx from github is a tampering signal
            if response.is_redirect or response.status_code in range(300, 400):
                raise RepoUrlCheckError(
                    "repo_url redirected unexpectedly; provide the canonical URL "
                    "(GitHub may redirect renamed repos — update the URL in your request)"
                )

            if response.status_code >= 400:
                raise RepoUrlCheckError(
                    f"repo_url HEAD check failed with status {response.status_code}; "
                    "verify the repository is public and accessible"
                )

            # Success — cache the positive result
            if redis is not None:
                with contextlib.suppress(Exception):
                    await redis.set(cache_key, "1", ex=settings.repo_head_cache_seconds)

            logger.debug("repo_head_check.ok", url=url, status=response.status_code)
            return

        except RepoUrlCheckError:
            raise
        except SSRFBlockedError as exc:
            # Never leak resolution details to the caller
            logger.warning("repo_head_check.ssrf_blocked", url=url, reason=str(exc))
            raise RepoUrlCheckError(
                "repo_url is not accessible: host resolves to a disallowed address"
            ) from exc
        except httpx.TimeoutException as exc:
            last_error = exc
            logger.warning("repo_head_check.timeout", url=url, attempt=attempt)
        except Exception as exc:
            last_error = exc
            logger.warning("repo_head_check.error", url=url, attempt=attempt, exc_info=True)

    raise RepoUrlCheckError(
        "repo_url HEAD check timed out or failed after retries; "
        "verify the repository is public and accessible"
    ) from last_error
