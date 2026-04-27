"""
SSRF-guarded HTTP transport for repo URL HEAD checks.

SSRFGuardedTransport subclasses httpx.AsyncHTTPTransport and rejects
requests whose target hostname resolves to any private/reserved IP address.

Defense layers:
  1. Hostname resolved via socket.getaddrinfo (sync, offloaded to thread pool).
     aiodns is optional — if installed it is used for async resolution.
  2. Every A + AAAA answer checked against PRIVATE_V4 + PRIVATE_V6 networks.
  3. follow_redirects=False on the httpx.AsyncClient — caller must enforce.
  4. IP literals in the URL itself are checked directly (no DNS lookup needed).

Private ranges blocked (IPv4):
  10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8,
  169.254.0.0/16 (link-local), 100.64.0.0/10 (CGNAT RFC6598), 0.0.0.0/8

Private ranges blocked (IPv6):
  ::1/128, fc00::/7 (ULA), fe80::/10 (link-local), ::/128

Usage:
    async with httpx.AsyncClient(
        transport=SSRFGuardedTransport(),
        follow_redirects=False,
        timeout=5.0,
    ) as client:
        resp = await client.head(url)
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Private IPv4 networks — block before TCP connect
PRIVATE_V4: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (RFC6598)
    ipaddress.ip_network("0.0.0.0/8"),
]

# Private IPv6 networks
PRIVATE_V6: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # ULA
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("::/128"),
]


class SSRFBlockedError(Exception):
    """Raised when the target IP is in a private/reserved range."""


def _is_private(ip_str: str) -> bool:
    """Return True if ip_str is in any private/reserved range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in net for net in PRIVATE_V4)
    if isinstance(addr, ipaddress.IPv6Address):
        return any(addr in net for net in PRIVATE_V6)
    return False


def _resolve_and_check(hostname: str) -> None:
    """Resolve hostname synchronously and raise SSRFBlockedError if private.

    Uses socket.getaddrinfo which handles both A and AAAA records.
    Called via asyncio.to_thread to avoid blocking the event loop.
    """
    try:
        results = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # Unresolvable host — treat as blocked (fail-closed)
        raise SSRFBlockedError(f"DNS resolution failed for {hostname!r}: {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip = sockaddr[0]
        if _is_private(ip):
            raise SSRFBlockedError(
                f"Host {hostname!r} resolved to private IP {ip!r} — SSRF blocked"
            )


def check_url_not_ssrf(url: str) -> None:
    """Synchronous SSRF check for a URL string.

    Extracts hostname from URL and checks DNS resolution.
    Raises SSRFBlockedError if any resolved IP is private.
    """
    parsed = httpx.URL(url)
    host = parsed.host

    # Handle IP literals directly — no DNS lookup needed
    try:
        addr = ipaddress.ip_address(host)
        if _is_private(str(addr)):
            raise SSRFBlockedError(f"URL contains private IP literal {host!r}")
        return
    except ValueError:
        pass  # Not an IP literal — proceed to DNS resolution

    _resolve_and_check(host)


class SSRFGuardedTransport(httpx.AsyncHTTPTransport):
    """httpx transport that checks DNS before connecting.

    Override handle_async_request to perform SSRF check on the resolved IP
    before allowing the actual connection to proceed.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host

        # Check IP literals inline
        try:
            addr = ipaddress.ip_address(host)
            if _is_private(str(addr)):
                raise SSRFBlockedError(f"Private IP literal blocked: {host!r}")
        except ValueError:
            # Not an IP literal — do DNS check in thread pool
            import asyncio  # noqa: PLC0415

            try:
                await asyncio.to_thread(_resolve_and_check, host)
            except SSRFBlockedError:
                raise
            except Exception as exc:
                raise SSRFBlockedError(f"SSRF pre-check failed: {exc}") from exc

        return await super().handle_async_request(request)


def make_ssrf_client(**kwargs: Any) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient with SSRF guard and no redirect following."""
    return httpx.AsyncClient(
        transport=SSRFGuardedTransport(),
        follow_redirects=False,
        **kwargs,
    )
