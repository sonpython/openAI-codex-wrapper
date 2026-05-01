"""
UsageTrackingMiddleware — post-response TPM true-up + monthly accounting (raw ASGI).

Red-team C3 fix: raw ASGI (NOT BaseHTTPMiddleware) so streaming bodies pass
through unbuffered.

Red-team C5 fix: TPM true-up uses per-window INCRBYFLOAT with a negative delta
when actual < estimated, correcting the upfront overcharge.  This is sound
because the same window key is used by both charge and true-up.

C1 fix: routes now set request.state.usage = {"prompt_tokens": ...,
"completion_tokens": ..., "total_tokens": ...} after response generation.
This middleware reads that value here to perform the true-up.  Without it,
TPM never decrements, monthly quota is never written, and billing has no
source of truth.

Responsibilities:
  1. Observe http.response.start status code.
  2. On final http.response.body chunk (more_body=False), if status < 400:
       a. TPM true-up: INCRBYFLOAT rl:tpm:{key}:{window_id} (actual - estimated)
       b. Monthly Redis increment: INCRBY monthly:{user}:{period} actual_total
       c. Fire-and-forget Postgres upsert via bg_session (best-effort).

Fail-open: all Redis and DB errors are swallowed after logging WARN.  True-up
failures are non-fatal; quota drift is bounded to the 60 s window and < 5 min
for plan cache staleness.

_BG_TASKS pattern from phase-01: module-level set prevents asyncio task GC.
H5 fix: _BG_TASKS is bounded at _BG_TASKS_MAX_SIZE.  When full, new tasks are
dropped with a WARN log rather than growing unbounded (Postgres saturation path).
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Any

import structlog
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from src.db.crud.usage_counter import increment as usage_increment
from src.db.crud.usage_daily import upsert as usage_daily_upsert
from src.db.engine import bg_session
from src.gateway.middleware.rate_limit import _month_start_utc
from src.redis_client import get_client
from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Prevent GC of fire-and-forget background tasks (phase-01 _BG_TASKS pattern).
_BG_TASKS: set[asyncio.Task[None]] = set()

# H5: cap to prevent unbounded growth when Postgres is saturated.
_BG_TASKS_MAX_SIZE = 1000

# Monthly Redis key TTL: 35 days covers the full month + a few days buffer.
_MONTHLY_TTL_SECONDS = 35 * 24 * 3600


class UsageTrackingMiddleware:
    """Raw ASGI post-response usage tracking and TPM true-up middleware.

    Observes the response status and final body chunk, then schedules
    background work to true-up TPM counters and persist monthly usage.
    Never blocks the response path.

    Redis is resolved lazily via get_client() so the class can be registered
    with add_middleware() before lifespan initialises the pool.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if settings.rate_limit_bypass:
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = scope.setdefault("state", {})
        user_id = state.get("user_id")
        api_key_id = state.get("api_key_id")

        if not api_key_id or not user_id:
            await self.app(scope, receive, send)
            return

        # Track status code across send calls (closure over mutable dict).
        _status: dict[str, int] = {"code": 0}

        async def send_wrap(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                _status["code"] = message.get("status", 0)

            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and _status["code"] < 400
            ):
                # H5: drop task if _BG_TASKS is at capacity (Postgres saturation).
                if len(_BG_TASKS) >= _BG_TASKS_MAX_SIZE:
                    logger.warning(
                        "usage_tracking.bg_tasks_full",
                        size=len(_BG_TASKS),
                        cap=_BG_TASKS_MAX_SIZE,
                    )
                else:
                    # Schedule true-up after the final response chunk is flushed.
                    task: asyncio.Task[None] = asyncio.create_task(
                        self._true_up(scope, str(user_id), str(api_key_id))
                    )
                    _BG_TASKS.add(task)
                    task.add_done_callback(_BG_TASKS.discard)

            await send(message)

        await self.app(scope, receive, send_wrap)

    async def _true_up(self, scope: MutableMapping[str, Any], user_str: str, key_str: str) -> None:
        """Perform TPM true-up and monthly accounting after response completes.

        All errors are swallowed after logging WARN — this is a best-effort
        background operation that must never affect the response the client
        already received.
        """
        try:
            state = scope.get("state", {})
            usage: dict[str, Any] | None = state.get("usage")

            est = float(state.get("tpm_estimated_cost", 0))
            window_id: int | None = state.get("tpm_window_id")

            redis = get_client()

            # TPM true-up: adjust the per-window counter by (actual - estimated).
            if usage and window_id is not None:
                actual_total = int(usage.get("total_tokens", 0))
                delta = float(actual_total) - est
                if delta != 0.0 and redis is not None:
                    tpm_key = f"rl:tpm:{key_str}:{window_id}"
                    try:
                        await redis.eval(  # type: ignore[no-untyped-call]
                            # Inline Lua: INCRBYFLOAT + PEXPIRE in one round-trip.
                            "redis.call('INCRBYFLOAT', KEYS[1], ARGV[1]);"
                            " redis.call('PEXPIRE', KEYS[1], 120000);"
                            " return 1",
                            1,
                            tpm_key,
                            str(delta),
                        )
                    except RedisError:
                        logger.warning("usage_tracking.tpm_trueup_failed", exc_info=True)
            else:
                actual_total = 0

            # Monthly Redis counter increment.
            period = _month_start_utc()
            monthly_key = f"monthly:{user_str}:{period}"
            if redis is not None:
                try:
                    if actual_total > 0:
                        await redis.incrby(monthly_key, actual_total)
                        await redis.expire(monthly_key, _MONTHLY_TTL_SECONDS)
                except RedisError:
                    logger.warning("usage_tracking.monthly_redis_failed", exc_info=True)

            # Postgres upsert — background pool, fire-and-forget.
            if usage and actual_total > 0:
                input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
                output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
                try:
                    import datetime  # noqa: PLC0415

                    today = datetime.datetime.now(datetime.UTC).date()
                    period_date = today.replace(day=1)
                    from uuid import UUID  # noqa: PLC0415

                    async with bg_session() as session:
                        await usage_increment(
                            session,
                            UUID(user_str),
                            period_date,
                            1,
                            input_tokens,
                            output_tokens,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning("usage_tracking.postgres_upsert_failed", exc_info=True)

            # Daily usage_daily upsert — separate session, best-effort.
            if usage and actual_total > 0 and key_str:
                input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
                output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
                try:
                    import datetime  # noqa: PLC0415

                    today = datetime.datetime.now(datetime.UTC).date()
                    from uuid import UUID  # noqa: PLC0415

                    async with bg_session() as session:
                        await usage_daily_upsert(
                            session,
                            user_id=UUID(user_str),
                            api_key_id=UUID(key_str),
                            period=today,
                            requests=1,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning("usage_tracking.daily_upsert_failed", exc_info=True)

        except Exception:  # noqa: BLE001
            logger.warning("usage_tracking.true_up_unexpected_error", exc_info=True)
