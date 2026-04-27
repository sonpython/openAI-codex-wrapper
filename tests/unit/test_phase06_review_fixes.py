"""
Tests for phase-06 code-review fixes:

  C1 — routes set request.state.usage; UsageTrackingMiddleware reads it for
       TPM true-up and monthly Redis increment.
  C2 — peek_and_estimate returns (None, None) when body > PEEK_MAX_BYTES; rate_limit
       middleware sends 413.
  H1 — Redis INCR error during concurrent check → DECR not called.
  H4 — GET request does not trigger receive() in peek_and_estimate.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# ── H4: GET request skips body peek ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_peek_and_estimate_skips_body_for_get() -> None:
    """GET /v1/chat/completions must not call receive() at all (H4 fix)."""
    from src.gateway.rate_limit_token_estimator import peek_and_estimate

    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/v1/chat/completions",
        "headers": [],
        "state": {},
    }

    called: list[bool] = []

    async def should_not_be_called() -> dict:  # type: ignore[type-arg]
        called.append(True)
        return {"type": "http.request", "body": b"", "more_body": False}

    cost, new_receive = await peek_and_estimate(scope, should_not_be_called)

    assert cost == 0, "GET should return 0 cost"
    assert not called, "receive() must NOT be called for GET"


@pytest.mark.asyncio
async def test_peek_and_estimate_peeks_body_for_post() -> None:
    """POST /v1/chat/completions DOES peek the body (normal path)."""
    from src.gateway.rate_limit_token_estimator import peek_and_estimate

    body = b'{"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}'
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"content-length", str(len(body)).encode())],
        "state": {},
    }

    called: list[bool] = []

    async def fake_receive() -> dict:  # type: ignore[type-arg]
        called.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    cost, new_receive = await peek_and_estimate(scope, fake_receive)

    assert cost > 0, "POST should estimate positive tokens"
    assert called, "receive() MUST be called for POST"


# ── C2: body cap returns sentinel ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peek_and_estimate_returns_none_when_body_exceeds_cap() -> None:
    """Body > PEEK_MAX_BYTES → sentinel (None, None) returned (C2 fix)."""
    from src.gateway.rate_limit_token_estimator import PEEK_MAX_BYTES, peek_and_estimate

    oversized = b"x" * (PEEK_MAX_BYTES + 1)
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [],
        "state": {},
    }

    call_count = 0

    async def chunked_receive() -> dict:  # type: ignore[type-arg]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"type": "http.request", "body": oversized, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    result = await peek_and_estimate(scope, chunked_receive)

    assert result == (None, None), "Oversized body must return sentinel"


@pytest.mark.asyncio
async def test_peek_and_estimate_content_length_fast_reject() -> None:
    """Content-Length > cap → 413 sentinel without reading any body bytes."""
    from src.gateway.rate_limit_token_estimator import PEEK_MAX_BYTES, peek_and_estimate

    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"content-length", str(PEEK_MAX_BYTES + 1).encode())],
        "state": {},
    }

    body_read = []

    async def should_not_read() -> dict:  # type: ignore[type-arg]
        body_read.append(True)
        return {"type": "http.request", "body": b"x", "more_body": False}

    result = await peek_and_estimate(scope, should_not_read)

    assert result == (None, None), "Content-Length over cap must short-circuit"
    assert not body_read, "No body chunks should be read when Content-Length exceeds cap"


@pytest.mark.asyncio
async def test_peek_and_estimate_under_cap_works_normally() -> None:
    """Body < PEEK_MAX_BYTES → normal estimate returned (not sentinel)."""
    from src.gateway.rate_limit_token_estimator import PEEK_MAX_BYTES, peek_and_estimate

    small_body = b'{"messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}'
    assert len(small_body) < PEEK_MAX_BYTES

    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"content-length", str(len(small_body)).encode())],
        "state": {},
    }

    async def fake_receive() -> dict:  # type: ignore[type-arg]
        return {"type": "http.request", "body": small_body, "more_body": False}

    cost, new_receive = await peek_and_estimate(scope, fake_receive)

    assert cost is not None and cost > 0
    assert new_receive is not None


# ── C2: rate_limit middleware returns 413 on sentinel ─────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_middleware_413_on_oversized_body() -> None:
    """Middleware returns 413 when peek_and_estimate signals oversized body."""
    from src.gateway.middleware.rate_limit import RateLimitMiddleware

    _FREE_LIMITS = {"rpm": 20, "tpm": 20000, "concurrent": 2, "monthly_tokens": 100000}

    responses: list[dict] = []  # type: ignore[type-arg]

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        # Route should never be reached when body is oversized.
        responses.append({"route_called": True})

    middleware = RateLimitMiddleware(inner)

    key_id = uuid4()
    user_id = uuid4()

    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [],
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tier": "free",
        },
    }

    sent_messages: list[dict] = []  # type: ignore[type-arg]

    async def send(message: dict) -> None:  # type: ignore[type-arg]
        sent_messages.append(message)

    async def receive() -> dict:  # type: ignore[type-arg]
        return {"type": "http.request", "body": b"", "more_body": False}

    fake_redis = fakeredis.aioredis.FakeRedis()

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch(
            "src.gateway.middleware.rate_limit.get_limits",
            new=AsyncMock(return_value=_FREE_LIMITS),
        ),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
        # Inject sentinel from peek_and_estimate
        patch(
            "src.gateway.middleware.rate_limit.peek_and_estimate",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
        await middleware(scope, receive, send)

    # Route must NOT have been called.
    assert not any(m.get("route_called") for m in responses), "Route should not be reached on 413"

    start_msg = next((m for m in sent_messages if m.get("type") == "http.response.start"), None)
    assert start_msg is not None, "Response start must be sent"
    assert start_msg["status"] == 413


# ── H1: Redis INCR error → no DECR ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_incr_redis_error_no_decr() -> None:
    """When INCR Lua raises RedisError, fail-open proceeds but DECR is NOT called (H1 fix)."""
    from redis.exceptions import RedisError
    from src.gateway.middleware.rate_limit import RateLimitMiddleware

    _FREE_LIMITS = {"rpm": 20, "tpm": 20000, "concurrent": 2, "monthly_tokens": 100000}

    fake_redis = fakeredis.aioredis.FakeRedis()
    decr_calls: list[str] = []

    # Patch decr on the fake_redis instance to track calls.
    original_decr = fake_redis.decr

    async def _tracking_decr(key: str) -> int:
        decr_calls.append(key)
        return await original_decr(key)

    fake_redis.decr = _tracking_decr  # type: ignore[method-assign]

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        from starlette.responses import Response

        await Response(content=b"ok", status_code=200)(scope, receive, send)

    middleware = RateLimitMiddleware(inner)

    # Mock only the _concurrent() method to raise RedisError — the rest of
    # the middleware (RPM/TPM Lua via fakeredis) runs normally.
    incr_script = AsyncMock(side_effect=RedisError("simulated INCR failure"))

    key_id = uuid4()
    user_id = uuid4()

    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/v1/ping",
        "query_string": b"",
        "headers": [],
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tier": "free",
        },
    }

    sent_messages: list[dict] = []  # type: ignore[type-arg]

    async def send(message: dict) -> None:  # type: ignore[type-arg]
        sent_messages.append(message)

    async def receive() -> dict:  # type: ignore[type-arg]
        return {"type": "http.request", "body": b"", "more_body": False}

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch(
            "src.gateway.middleware.rate_limit.get_limits",
            new=AsyncMock(return_value=_FREE_LIMITS),
        ),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
        # Patch _concurrent to return the failing script.
        patch.object(middleware, "_concurrent", return_value=incr_script),
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
        await middleware(scope, receive, send)

    # H1: DECR must NOT be called — INCR raised RedisError, counter was never touched.
    assert not decr_calls, f"DECR must not be called when INCR raised RedisError, got: {decr_calls}"

    # Fail-open: request must have been passed through (200).
    start_msg = next((m for m in sent_messages if m.get("type") == "http.response.start"), None)
    assert start_msg is not None
    assert start_msg["status"] == 200


# ── C1: route sets state.usage; usage_tracking reads it ──────────────────────


@pytest.mark.asyncio
async def test_usage_tracking_reads_state_usage_for_tpm_trueup() -> None:
    """UsageTrackingMiddleware._true_up reads state['usage'] to correct TPM counter."""
    from src.gateway.middleware.usage_tracking import UsageTrackingMiddleware

    key_id = str(uuid4())
    user_id = str(uuid4())
    window_id = 12345  # arbitrary

    # Build a scope with state populated by the route (C1 contract).
    scope: dict = {
        "type": "http",
        "path": "/v1/chat/completions",
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tpm_estimated_cost": 1000.0,
            "tpm_window_id": window_id,
            # C1: route sets this after response generation.
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        },
    }

    eval_calls: list[tuple] = []
    incrby_calls: list[tuple] = []

    class _FakeRedis:
        async def eval(self, script: str, numkeys: int, *args: object) -> int:
            eval_calls.append((script, args))
            return 1

        async def incrby(self, key: str, amount: int) -> int:
            incrby_calls.append((key, amount))
            return amount

        async def expire(self, key: str, seconds: int) -> bool:
            return True

    fake_redis = _FakeRedis()

    with (
        patch("src.gateway.middleware.usage_tracking.get_client", return_value=fake_redis),
        patch("src.gateway.middleware.usage_tracking.get_settings") as mock_settings,
        patch("src.gateway.middleware.usage_tracking.bg_session") as mock_bg,
        patch("src.gateway.middleware.usage_tracking.usage_increment", new=AsyncMock()),
    ):
        mock_settings.return_value.rate_limit_bypass = False
        mock_bg.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_bg.return_value.__aexit__ = AsyncMock(return_value=False)

        middleware = UsageTrackingMiddleware.__new__(UsageTrackingMiddleware)
        await middleware._true_up(scope, user_id, key_id)

    # TPM eval (INCRBYFLOAT) must have been called with delta = actual - estimated
    # actual_total = 150, estimated = 1000 → delta = -850.0
    assert eval_calls, "INCRBYFLOAT Lua eval must be called when usage and window_id are present"
    _script, args = eval_calls[0]
    tpm_key_arg = args[0]
    delta_arg = float(args[1])
    assert f"rl:tpm:{key_id}:{window_id}" == tpm_key_arg
    assert abs(delta_arg - (-850.0)) < 0.01, f"Expected delta -850, got {delta_arg}"

    # Monthly INCRBY must have been called with actual total tokens.
    assert incrby_calls, "Monthly INCRBY must be called after TPM true-up"
    _monthly_key, monthly_amount = incrby_calls[0]
    assert (
        monthly_amount == 150
    ), f"Monthly increment must equal actual_total=150, got {monthly_amount}"


@pytest.mark.asyncio
async def test_usage_tracking_no_tpm_trueup_when_usage_not_set() -> None:
    """When state['usage'] is absent (route didn't set it), no INCRBYFLOAT is called."""
    from src.gateway.middleware.usage_tracking import UsageTrackingMiddleware

    key_id = str(uuid4())
    user_id = str(uuid4())

    scope: dict = {
        "type": "http",
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tpm_estimated_cost": 500.0,
            "tpm_window_id": 99999,
            # No 'usage' key — simulates old behavior (C1 bug scenario).
        },
    }

    eval_calls: list = []

    class _FakeRedis:
        async def eval(self, *args: object) -> int:
            eval_calls.append(args)
            return 1

        async def incrby(self, key: str, amount: int) -> int:
            return amount

        async def expire(self, key: str, seconds: int) -> bool:
            return True

    with (
        patch("src.gateway.middleware.usage_tracking.get_client", return_value=_FakeRedis()),
        patch("src.gateway.middleware.usage_tracking.get_settings") as mock_settings,
    ):
        mock_settings.return_value.rate_limit_bypass = False
        middleware = UsageTrackingMiddleware.__new__(UsageTrackingMiddleware)
        await middleware._true_up(scope, user_id, key_id)

    assert not eval_calls, "INCRBYFLOAT must NOT be called when state['usage'] is absent"


@pytest.mark.asyncio
async def test_usage_tracking_bg_tasks_bounded() -> None:
    """_BG_TASKS is capped at _BG_TASKS_MAX_SIZE — excess tasks are dropped with WARN."""
    import src.gateway.middleware.usage_tracking as ut_module
    from src.gateway.middleware.usage_tracking import _BG_TASKS_MAX_SIZE, UsageTrackingMiddleware

    original_bg_tasks = ut_module._BG_TASKS

    try:
        # Replace with a fake set that's already full.
        full_set: set = set()
        for _ in range(_BG_TASKS_MAX_SIZE):
            # Use a sentinel non-Task object so len() works without asyncio overhead.
            full_set.add(object())
        ut_module._BG_TASKS = full_set  # type: ignore[assignment]

        async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
            from starlette.responses import Response

            await Response(content=b"ok", status_code=200)(scope, receive, send)

        middleware = UsageTrackingMiddleware(inner)

        key_id = uuid4()
        user_id = uuid4()

        scope: dict = {
            "type": "http",
            "path": "/v1/chat/completions",
            "state": {
                "api_key_id": key_id,
                "user_id": user_id,
            },
        }

        sent: list[dict] = []  # type: ignore[type-arg]

        async def send(message: dict) -> None:  # type: ignore[type-arg]
            sent.append(message)

        async def receive() -> dict:  # type: ignore[type-arg]
            return {"type": "http.request", "body": b"", "more_body": False}

        # Count tasks before and after — set should not grow.
        size_before = len(ut_module._BG_TASKS)

        with patch("src.gateway.middleware.usage_tracking.get_settings") as mock_settings:
            mock_settings.return_value.rate_limit_bypass = False

            await middleware(scope, receive, send)

        size_after = len(ut_module._BG_TASKS)
        assert (
            size_after == size_before
        ), f"_BG_TASKS must not grow when at cap: before={size_before}, after={size_after}"

    finally:
        ut_module._BG_TASKS = original_bg_tasks  # type: ignore[assignment]
