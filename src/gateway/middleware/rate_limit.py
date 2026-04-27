"""
RateLimitMiddleware — per-API-key RPM/TPM/concurrent enforcement (raw ASGI).

Red-team C3 fix: implemented as raw ASGI (NOT BaseHTTPMiddleware) so SSE /
StreamingResponse bodies pass through unbuffered.  BaseHTTPMiddleware buffers
the entire body before sending, breaking first-byte latency on streaming routes
(Starlette #1012, FastAPI #5536).

Red-team C4 fix: concurrent counter TTL is refreshed every 30 s via a
background asyncio task (_refresh_concurrent_ttl) for streams > 60 s.
Without this, a 90 s stream would let the TTL expire mid-flight and silently
allow a 3rd concurrent request under a cap of 2.

Red-team C5 fix: TPM uses per-window INCRBYFLOAT counter (tpm_check.lua),
not a ZSET with negative entries.  True-up after response subtracts the
overestimate via a negative INCRBYFLOAT from UsageTrackingMiddleware.

Fail-open on Redis errors: logged at WARN, request passes through.  A Redis
outage should not cause a total service blackout; the trade-off (temporary
uncapped traffic) is documented and accepted for v1.

Middleware ordering (registration in app.py):
    add_middleware(UsageTracking)   # innermost — closest to route
    add_middleware(RateLimit)       # this file
    add_middleware(Auth)
    add_middleware(EdgeIPLimiter)   # outermost — first on request

REQUEST flow: EdgeIPLimiter → Auth → RateLimit → UsageTracking → route
RESPONSE flow reverses automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from collections.abc import MutableMapping
from typing import Any

import structlog
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from src.db.crud.plans import get_limits
from src.db.engine import main_session
from src.gateway.rate_limit_errors import send_429
from src.gateway.rate_limit_reset_format import format_reset_ms
from src.gateway.rate_limit_token_estimator import peek_and_estimate
from src.infra.redis_lua import load_script
from src.observability.metrics import RATE_LIMIT_REJECTIONS
from src.redis_client import get_client
from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Paths that bypass rate limiting entirely.
_SKIP_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})
_SKIP_PREFIXES: tuple[str, ...] = ("/admin/",)

# Concurrent TTL refresh interval for long streams (C4 fix).
_CONCURRENT_REFRESH_INTERVAL = 30  # seconds
_CONCURRENT_TTL_MS = 60_000  # 60 s TTL on the concurrent counter key


def _should_skip(path: str) -> bool:
    if path in _SKIP_PATHS:
        return True
    return any(path.startswith(p) for p in _SKIP_PREFIXES)


def _now_ms() -> int:
    return int(time.time() * 1000)


class RateLimitMiddleware:
    """Raw ASGI rate-limit middleware.

    Enforces per-API-key limits across three dimensions on every request:
      1. RPM  — sliding-window via ZSET (sliding-window.lua)
      2. TPM  — per-window INCRBYFLOAT counter (tpm_check.lua)
      3. Concurrent — atomic INCR/DECR with TTL refresh (concurrent_check.lua)

    Monthly quota is checked here (Redis cache) and decremented by
    UsageTrackingMiddleware after the response completes.

    Headers stashed on scope["state"]["rate_limit_headers"] are read by:
      - stream routes: merged into StreamingResponse(headers=...) at construction
      - sync routes:   injected into the ASGI send wrapper here (belt-and-suspenders)

    Redis is resolved lazily via get_client() so the class can be registered
    with add_middleware() before lifespan initialises the pool.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._sw_script: Any = None
        self._tpm_script: Any = None
        self._concurrent_script: Any = None

    def _sw(self) -> Any:
        redis = get_client()
        if redis is None:
            return None
        if self._sw_script is None:
            self._sw_script = load_script(redis, "sliding-window")
        return self._sw_script

    def _tpm(self) -> Any:
        redis = get_client()
        if redis is None:
            return None
        if self._tpm_script is None:
            self._tpm_script = load_script(redis, "tpm_check")
        return self._tpm_script

    def _concurrent(self) -> Any:
        redis = get_client()
        if redis is None:
            return None
        if self._concurrent_script is None:
            self._concurrent_script = load_script(redis, "concurrent_check")
        return self._concurrent_script

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if _should_skip(path):
            await self.app(scope, receive, send)
            return

        settings = get_settings()

        # RATE_LIMIT_BYPASS=true skips all enforcement (dev/test).
        # In prod this is refused at settings validation time.
        if settings.rate_limit_bypass:
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = scope.setdefault("state", {})
        api_key_id: Any = state.get("api_key_id")
        if not api_key_id:
            # AuthMiddleware didn't populate state — request will be rejected
            # downstream; no rate-limit action needed.
            await self.app(scope, receive, send)
            return

        user_id = state.get("user_id")
        tier: str = str(state.get("tier", "free"))
        key_str = str(api_key_id)
        user_str = str(user_id)

        # Load tier limits from in-process cache (5-min TTL, DB fallback).
        try:
            async with main_session() as session:
                limits = await get_limits(session, tier)
        except Exception:  # noqa: BLE001
            logger.warning("rate_limit.get_limits_failed", tier=tier, exc_info=True)
            await self.app(scope, receive, send)
            return

        redis = get_client()
        if redis is None:
            # Redis not yet initialised (pre-lifespan) — fail-open.
            await self.app(scope, receive, send)
            return

        # ── Monthly quota check (Redis cache, fail-open) ───────────────────
        try:
            period = _month_start_utc()
            monthly_key = f"monthly:{user_str}:{period}"
            cached_monthly = await redis.get(monthly_key)
            if cached_monthly is not None:
                monthly_used = int(float(cached_monthly))
                if monthly_used >= limits["monthly_tokens"]:
                    retry = _seconds_until_next_month()
                    RATE_LIMIT_REJECTIONS.labels(dimension="monthly").inc()
                    await send_429(send, "monthly_quota_exceeded", retry_after_seconds=retry)
                    return
        except RedisError:
            logger.warning("rate_limit.monthly_check_redis_error", exc_info=True)
            # Fail-open: proceed without monthly check.

        # ── Concurrent counter — atomic INCR (Lua) ─────────────────────────
        concurrent_key = f"rl:concurrent:{key_str}"
        # H1: Track whether INCR actually ran so we only DECR when it did.
        # If RedisError occurs during INCR we fail-open (ok=True) but must
        # NOT DECR a key that was never incremented — that drifts counter to -1.
        _incr_succeeded = False
        try:
            concurrent_script = self._concurrent()
            if concurrent_script is None:
                ok = True
            else:
                ok = await concurrent_script(
                    keys=[concurrent_key],
                    args=[str(limits["concurrent"]), str(_CONCURRENT_TTL_MS)],
                )
                _incr_succeeded = True  # Lua call completed (whether allowed or denied)
                if not ok:
                    RATE_LIMIT_REJECTIONS.labels(dimension="concurrent").inc()
                    await send_429(send, "concurrent_limit_exceeded", retry_after_seconds=1)
                    return
        except RedisError:
            logger.warning("rate_limit.concurrent_check_redis_error", exc_info=True)
            # Fail-open: skip concurrent check. _incr_succeeded stays False.
            ok = True

        # Schedule periodic TTL refresh for long-running streams (C4 fix).
        refresh_task: asyncio.Task[None] | None = None
        if ok:
            refresh_task = asyncio.create_task(self._refresh_concurrent_ttl(concurrent_key))

        try:
            # ── Token estimation (body peek + replay shim) ─────────────────
            try:
                peek_result = await peek_and_estimate(scope, receive)
            except Exception:  # noqa: BLE001
                logger.warning("rate_limit.estimation_failed", exc_info=True)
                peek_result = (0, receive)

            # C2: sentinel (None, None) means body exceeded cap → 413.
            if peek_result[0] is None:
                from src.gateway.rate_limit_errors import _openai_error_response  # noqa: PLC0415

                await _openai_error_response(
                    send,
                    413,
                    "Request body too large. Maximum allowed: 256 KB.",
                    error_type="invalid_request_error",
                    code="request_too_large",
                )
                return

            est, receive = peek_result

            now = _now_ms()
            window_id = int(time.time()) // 60

            # ── RPM sliding window ─────────────────────────────────────────
            sw_script = self._sw()
            try:
                if sw_script is None:
                    raise RedisError("redis not available")
                rpm_result = await sw_script(
                    keys=[f"rl:rpm:{key_str}"],
                    args=[str(now), "60000", str(limits["rpm"]), str(uuid.uuid4())],
                )
                rpm_allowed = int(rpm_result[0]) if rpm_result else 1
                rpm_remaining = int(rpm_result[2]) if rpm_result else limits["rpm"]
                rpm_reset_ms = int(rpm_result[3]) if rpm_result else 60_000
            except RedisError:
                logger.warning("rate_limit.rpm_check_redis_error", exc_info=True)
                rpm_allowed, rpm_remaining, rpm_reset_ms = 1, limits["rpm"], 60_000

            if not rpm_allowed:
                retry = max(1, math.ceil(rpm_reset_ms / 1000))
                RATE_LIMIT_REJECTIONS.labels(dimension="rpm").inc()
                await send_429(send, "rpm_exceeded", retry_after_seconds=retry)
                return

            # ── TPM per-window counter ─────────────────────────────────────
            tpm_key = f"rl:tpm:{key_str}:{window_id}"
            tpm_script = self._tpm()
            try:
                if tpm_script is None:
                    raise RedisError("redis not available")
                tpm_result = await tpm_script(
                    keys=[tpm_key],
                    args=[str(now), "60000", str(limits["tpm"]), str(float(est))],
                )
                tpm_allowed = int(tpm_result[0]) if tpm_result else 1
                tpm_remaining = float(tpm_result[2]) if tpm_result else float(limits["tpm"])
                tpm_reset_ms = int(tpm_result[3]) if tpm_result else 60_000
            except RedisError:
                logger.warning("rate_limit.tpm_check_redis_error", exc_info=True)
                tpm_allowed = 1
                tpm_remaining, tpm_reset_ms = float(limits["tpm"]), 60_000

            if not tpm_allowed:
                retry = max(1, math.ceil(tpm_reset_ms / 1000))
                RATE_LIMIT_REJECTIONS.labels(dimension="tpm").inc()
                await send_429(send, "tpm_exceeded", retry_after_seconds=retry)
                return

            # ── Stash headers dict for routes + send-wrap ─────────────────
            rl_headers: dict[str, str] = {
                "X-RateLimit-Limit-Requests": str(limits["rpm"]),
                "X-RateLimit-Remaining-Requests": str(rpm_remaining),
                "X-RateLimit-Reset-Requests": format_reset_ms(rpm_reset_ms),
                "X-RateLimit-Limit-Tokens": str(limits["tpm"]),
                "X-RateLimit-Remaining-Tokens": str(int(tpm_remaining)),
                "X-RateLimit-Reset-Tokens": format_reset_ms(tpm_reset_ms),
            }
            state["rate_limit_headers"] = rl_headers
            state["tpm_estimated_cost"] = est
            state["tpm_window_id"] = window_id
            state["_concurrent_key"] = concurrent_key

            # Belt-and-suspenders send wrapper: injects headers on
            # http.response.start for sync routes.  Stream routes also set
            # headers at EventSourceResponse construction time (C3 contract).
            async def send_with_headers(message: MutableMapping[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    for k, v in rl_headers.items():
                        headers.append((k.lower().encode(), v.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_headers)

        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                    await refresh_task
            # H1: Only DECR when INCR actually ran.  If the Lua call raised
            # RedisError (_incr_succeeded=False), the counter was never touched
            # — decrementing would drift it to -1 on the next recovery call.
            if ok and _incr_succeeded:
                _redis = get_client()
                if _redis is not None:
                    try:
                        await _redis.decr(concurrent_key)
                    except RedisError:
                        logger.warning(
                            "rate_limit.concurrent_decr_failed",
                            key=concurrent_key,
                            exc_info=True,
                        )

    async def _refresh_concurrent_ttl(self, key: str) -> None:
        """Refresh the concurrent counter TTL every 30 s.

        Long SSE streams routinely exceed 60 s.  Without refresh the counter
        TTL would expire mid-stream, allowing the next request to see count=1
        instead of count=2 (for cap=2), silently bypassing the concurrent cap
        (red-team C4 fix).  This task is cancelled in the finally block.

        H2 fixes:
          - Time unit: use PEXPIRE with ms (_CONCURRENT_TTL_MS=60_000) not
            expire with seconds (60 seconds is the same value, but pexpire
            keeps unit consistency with the rest of the file).
          - Transient RedisError: log WARN and continue the loop instead of
            exiting permanently. A single blip should not kill TTL refresh
            for the entire stream duration.
        """
        try:
            while True:
                await asyncio.sleep(_CONCURRENT_REFRESH_INTERVAL)
                _redis = get_client()
                if _redis is not None:
                    try:
                        # H2: pexpire (ms) is consistent with _CONCURRENT_TTL_MS unit.
                        await _redis.pexpire(key, _CONCURRENT_TTL_MS)
                    except RedisError:
                        # H2: transient error — log WARN and keep looping.
                        logger.warning(
                            "rate_limit.concurrent_ttl_refresh_failed",
                            key=key,
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            return


def _month_start_utc() -> str:
    """Return the first day of the current UTC month as 'YYYY-MM-01'."""
    import datetime

    today = datetime.datetime.now(datetime.UTC).date()
    return today.replace(day=1).isoformat()


def _seconds_until_next_month() -> int:
    """Return seconds until the first day of next UTC month (for Retry-After)."""
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0)
    else:
        next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0)
    return max(1, int((next_month - now).total_seconds()))
