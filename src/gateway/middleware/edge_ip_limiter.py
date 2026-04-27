"""
EdgeIPLimiter — pre-auth per-IP rate-limit middleware (raw ASGI).

Red-team C2 fix: unauthenticated requests with missing or malformed
Authorization headers would otherwise pass through AuthMiddleware (which
calls argon2id verify at ~30-100ms/attempt), enabling a DoS that burns CPU
on garbage tokens.  This middleware intercepts BEFORE AuthMiddleware, checks
the bearer token shape, and buckets per-IP when the shape is wrong.

Middleware ordering (outermost = first on request):
    EdgeIPLimiter → AuthMiddleware → RateLimitMiddleware → UsageTracking → route

Registration in app.py (last add_middleware call = outermost wrap):
    app.add_middleware(EdgeIPLimiter)   # added last, runs first

IP source:
    TRUST_PROXY=true  → X-Forwarded-For LAST hop (proxy-trusted) — H3 fix.
    TRUST_PROXY=false → scope["client"][0] (direct TCP peer)

H3 XFF security note:
    Caddy by default APPENDS to X-Forwarded-For (does not replace).
    The leftmost hop in XFF is client-controlled and trivially spoofable:
        curl -H "X-Forwarded-For: 1.2.3.4" ...
    The LAST hop is the address Caddy observed at the TCP layer — that is
    the one the attacker cannot spoof without controlling the network path.
    We therefore trust the LAST comma-separated entry when TRUST_PROXY=true.

    DEPLOYMENT REQUIREMENT: TRUST_PROXY=true REQUIRES your reverse proxy
    to be the ONLY hop adding to XFF, or to overwrite XFF entirely via:
        header_up X-Forwarded-For {http.request.remote.host}
    Without this, an upstream proxy could still inject spoofed entries
    before Caddy's appended IP. Single-proxy deployments behind Caddy are
    safe with the last-hop approach. See .env.example for details.

Fail-open: any Redis error is logged at WARN level and the request passes
through.  A Redis outage should not cause a total service blackout.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from src.gateway.rate_limit_errors import send_429
from src.infra.redis_lua import load_script
from src.redis_client import get_client
from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Paths that bypass IP rate limiting entirely — health/metrics probes.
_SKIP_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})

# Valid bearer token shape: "Bearer cwk_<24+ alphanumeric/dash/underscore>"
# Compiled once at module load — O(1) per check.
_BEARER_RE = re.compile(rb"^[Bb]earer\s+cwk_[A-Za-z0-9_-]{24,}$")


class EdgeIPLimiter:
    """Raw ASGI pre-auth IP rate-limit middleware.

    Runs before AuthMiddleware.  Requests with a missing or malformed
    Authorization header are counted per source IP; once IP_PRE_AUTH_RPM
    is exceeded in a 60-second window the request is rejected with 429
    before any argon2 work is performed.

    Requests with a correctly-shaped bearer token pass through immediately
    (AuthMiddleware will do the real cryptographic verify).

    Redis client is resolved lazily on each request via get_client() so this
    class can be registered with add_middleware() before lifespan starts.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._script: Any = None  # lazy-loaded on first request

    def _get_script(self) -> Any:
        redis = get_client()
        if redis is None:
            return None
        if self._script is None:
            self._script = load_script(redis, "edge_ip_check")
        return self._script

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        auth = _get_header(scope, b"authorization")

        # If the token looks like a valid cwk_ bearer, skip IP bucketing.
        # AuthMiddleware will cryptographically verify it.
        if auth and _BEARER_RE.match(auth):
            await self.app(scope, receive, send)
            return

        # Missing or malformed token — check the per-IP bucket.
        ip = _client_ip(scope, settings.trust_proxy)
        redis = get_client()
        if redis is None:
            # Redis not yet initialised (startup) — fail-open.
            await self.app(scope, receive, send)
            return

        try:
            script = self._get_script()
            if script is None:
                await self.app(scope, receive, send)
                return
            result = await script(
                keys=[f"ip_pre_auth:{ip}"],
                args=[str(settings.ip_pre_auth_rpm), "60000"],
            )
            allowed = int(result) if result is not None else 1
        except RedisError:
            logger.warning("edge_ip_limiter.redis_error", ip=ip, exc_info=True)
            # Fail-open: let the request through; AuthMiddleware will 401 it.
            await self.app(scope, receive, send)
            return

        if not allowed:
            logger.info("edge_ip_limiter.rejected", ip=ip)
            await send_429(send, "ip_pre_auth_exceeded", retry_after_seconds=60)
            return

        await self.app(scope, receive, send)


def _get_header(scope: Scope, name: bytes) -> bytes | None:
    """Extract a single header value from ASGI scope headers."""
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return bytes(v)
    return None


def _client_ip(scope: Scope, trust_proxy: bool) -> str:
    """Resolve the client IP address.

    With TRUST_PROXY=true, reads the LAST hop from X-Forwarded-For (H3 fix).
    Caddy appends the real client IP to XFF — the last entry is what Caddy
    observed at the TCP layer, which cannot be spoofed by the client sending
    a forged XFF header. The first entry is client-controlled and MUST NOT
    be trusted.

    Without TRUST_PROXY, uses the raw TCP peer address from scope["client"].

    Falls back to "unknown" if neither is available.
    """
    if trust_proxy:
        xff = _get_header(scope, b"x-forwarded-for")
        if xff:
            hops = xff.decode("latin-1").split(",")
            # H3: last hop is the proxy-appended value — trust it, not [0].
            return hops[-1].strip()
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"
