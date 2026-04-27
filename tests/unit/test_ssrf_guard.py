"""
Tests for SSRF guard transport and repo URL head check.

Covers:
  - _is_private: correctly identifies RFC1918, 127.x, 169.254.x, CGNAT, IPv6 private
  - check_url_not_ssrf: raises SSRFBlockedError for private IP literals
  - SSRFGuardedTransport: rejects hostname resolving to private IP (mocked DNS)
  - check_repo_url: 3xx redirect → RepoUrlCheckError
  - check_repo_url: cache hit skips HTTP call
  - check_repo_url: SSRF blocked → RepoUrlCheckError (no details leaked)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.gateway.ssrf_transport import (
    SSRFBlockedError,
    _is_private,
    check_url_not_ssrf,
)
from src.workers.repo_url_head_check import RepoUrlCheckError, check_repo_url

# ── _is_private unit tests ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "127.0.0.1",
        "127.255.255.255",
        "169.254.0.1",  # link-local
        "100.64.0.1",  # CGNAT
        "0.0.0.1",  # 0/8
        "::1",  # IPv6 loopback
        "fc00::1",  # ULA
        "fe80::1",  # link-local IPv6
    ],
)
def test_is_private_returns_true(ip):
    assert _is_private(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "140.82.114.4",  # github.com
        "2606:4700::6810:84e5",  # public IPv6
    ],
)
def test_is_private_returns_false_for_public(ip):
    assert _is_private(ip) is False


# ── check_url_not_ssrf ────────────────────────────────────────────────────────


def test_check_url_blocks_private_ip_literal():
    with pytest.raises(SSRFBlockedError):
        check_url_not_ssrf("https://127.0.0.1/repo")


def test_check_url_blocks_cgnat_literal():
    with pytest.raises(SSRFBlockedError):
        check_url_not_ssrf("https://100.64.0.1/repo")


def test_check_url_allows_public_hostname(monkeypatch):
    """Public hostname: DNS resolution returns public IP → no error."""
    import socket

    def mock_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("140.82.114.4", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    # Should not raise
    check_url_not_ssrf("https://github.com/owner/repo")


def test_check_url_blocks_hostname_resolving_to_private(monkeypatch):
    """Hostname resolving to 127.0.0.1 → SSRFBlockedError."""
    import socket

    def mock_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(SSRFBlockedError):
        check_url_not_ssrf("https://evil-public-looking.test/repo")


# ── check_repo_url ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_repo_url_cache_hit_skips_http():
    """Redis cache hit → no HTTP request made."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"1")  # cache hit

    with patch("src.workers.repo_url_head_check.make_ssrf_client") as mock_client_fn:
        await check_repo_url("https://github.com/owner/repo", redis=mock_redis)
        mock_client_fn.assert_not_called()


@pytest.mark.asyncio
async def test_check_repo_url_3xx_redirect_raises():
    """3xx response → RepoUrlCheckError (not followed)."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    mock_response = MagicMock()
    mock_response.is_redirect = True
    mock_response.status_code = 301

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.head = AsyncMock(return_value=mock_response)

    with (
        patch("src.workers.repo_url_head_check.make_ssrf_client", return_value=mock_client),
        pytest.raises(RepoUrlCheckError) as exc_info,
    ):
        await check_repo_url("https://github.com/owner/repo", redis=mock_redis)

    assert "redirect" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_check_repo_url_ssrf_blocked_raises_without_leaking_details():
    """SSRF block → RepoUrlCheckError; message must not contain IP details."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.head = AsyncMock(side_effect=SSRFBlockedError("resolved to 127.0.0.1"))

    with (
        patch("src.workers.repo_url_head_check.make_ssrf_client", return_value=mock_client),
        pytest.raises(RepoUrlCheckError) as exc_info,
    ):
        await check_repo_url("https://github.com/owner/repo", redis=mock_redis)

    # Error message shown to caller must not contain internal resolution details
    assert "127.0.0.1" not in str(exc_info.value)
    assert "disallowed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_check_repo_url_success_caches_result():
    """Successful HEAD → result written to Redis cache."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()

    mock_response = MagicMock()
    mock_response.is_redirect = False
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.head = AsyncMock(return_value=mock_response)

    mock_settings = MagicMock(repo_head_timeout=5, repo_head_cache_seconds=300)
    with (
        patch("src.workers.repo_url_head_check.make_ssrf_client", return_value=mock_client),
        patch("src.settings.get_settings", return_value=mock_settings),
    ):
        await check_repo_url("https://github.com/owner/repo", redis=mock_redis)

    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    # Redis.set called with (key, value, ex=300) — check ex kwarg or positional
    assert call_args.kwargs.get("ex") == 300 or 300 in call_args.args
