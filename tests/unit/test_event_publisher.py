"""
Unit tests for workers/event_publisher.py.

Uses an async mock Redis client — no real Redis required.
Verifies:
  - RPUSH + EXPIRE + PUBLISH called in correct order via pipeline
  - JSON payload shape (type, job_id, ts, merged payload)
  - Terminal event type set matches spec
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.workers.event_publisher import (  # noqa: E402
    TERMINAL_EVENT_TYPES,
    publish_job_event,
)

# ── Mock Redis helpers ────────────────────────────────────────────────────────


def _make_mock_redis() -> MagicMock:
    """Build a mock redis.asyncio.Redis with a working async pipeline context."""
    pipe = MagicMock()
    pipe.rpush = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.publish = MagicMock(return_value=None)
    pipe.execute = AsyncMock(return_value=[1, True, 1])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    return redis, pipe


# ── payload shape ─────────────────────────────────────────────────────────────


async def test_publish_event_has_required_fields() -> None:
    redis, pipe = _make_mock_redis()
    job_id = "job-123"

    await publish_job_event(redis, job_id, "job.started", {"extra": "value"})

    pipe.execute.assert_awaited_once()
    # Extract the raw JSON that was passed to rpush
    rpush_call_args = pipe.rpush.call_args
    assert rpush_call_args is not None
    raw_json = rpush_call_args[0][1]  # positional: (list_key, raw)
    payload = json.loads(raw_json)

    assert payload["type"] == "job.started"
    assert payload["job_id"] == job_id
    assert "ts" in payload
    assert payload["extra"] == "value"


async def test_publish_event_ts_is_iso_format() -> None:
    redis, pipe = _make_mock_redis()
    await publish_job_event(redis, "j1", "job.queued", {})

    raw_json = pipe.rpush.call_args[0][1]
    payload = json.loads(raw_json)
    ts = payload["ts"]
    # Must be parseable ISO format
    from datetime import datetime  # noqa: PLC0415

    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert dt is not None


# ── Redis key patterns ────────────────────────────────────────────────────────


async def test_publish_uses_correct_list_key() -> None:
    redis, pipe = _make_mock_redis()
    job_id = "abc-def-123"
    await publish_job_event(redis, job_id, "job.started", {})

    rpush_key = pipe.rpush.call_args[0][0]
    assert rpush_key == f"job:events:list:{job_id}"


async def test_publish_uses_correct_channel_key() -> None:
    redis, pipe = _make_mock_redis()
    job_id = "abc-def-123"
    await publish_job_event(redis, job_id, "job.started", {})

    publish_key = pipe.publish.call_args[0][0]
    assert publish_key == f"job:events:{job_id}"


async def test_publish_expire_ttl_is_24h() -> None:
    redis, pipe = _make_mock_redis()
    await publish_job_event(redis, "j1", "job.started", {})

    expire_args = pipe.expire.call_args[0]
    assert expire_args[1] == 86_400  # 24h in seconds


# ── pipeline ordering ─────────────────────────────────────────────────────────


async def test_pipeline_calls_rpush_expire_publish_in_order() -> None:
    """rpush, expire, publish must all be called before execute."""
    redis, pipe = _make_mock_redis()
    call_order: list[str] = []

    pipe.rpush.side_effect = lambda *a, **kw: call_order.append("rpush")
    pipe.expire.side_effect = lambda *a, **kw: call_order.append("expire")
    pipe.publish.side_effect = lambda *a, **kw: call_order.append("publish")

    await publish_job_event(redis, "j1", "job.started", {})

    assert call_order == ["rpush", "expire", "publish"]
    pipe.execute.assert_awaited_once()


# ── JSON of publish matches rpush ─────────────────────────────────────────────


async def test_rpush_and_publish_carry_same_json() -> None:
    redis, pipe = _make_mock_redis()
    await publish_job_event(redis, "j1", "job.completed", {"result": "ok"})

    rpush_raw = pipe.rpush.call_args[0][1]
    publish_raw = pipe.publish.call_args[0][1]
    assert rpush_raw == publish_raw


# ── TERMINAL_EVENT_TYPES ──────────────────────────────────────────────────────


def test_terminal_types_contains_expected() -> None:
    assert "job.completed" in TERMINAL_EVENT_TYPES
    assert "job.failed" in TERMINAL_EVENT_TYPES
    assert "job.cancelled" in TERMINAL_EVENT_TYPES


def test_non_terminal_types_not_in_set() -> None:
    assert "job.started" not in TERMINAL_EVENT_TYPES
    assert "job.queued" not in TERMINAL_EVENT_TYPES
    assert "job.codex_event" not in TERMINAL_EVENT_TYPES
    assert "job.diff_ready" not in TERMINAL_EVENT_TYPES


# ── payload merge ─────────────────────────────────────────────────────────────


async def test_publish_merges_extra_payload() -> None:
    redis, pipe = _make_mock_redis()
    await publish_job_event(redis, "j1", "job.failed", {"error": "timeout", "code": 124})

    raw_json = pipe.rpush.call_args[0][1]
    payload = json.loads(raw_json)
    assert payload["error"] == "timeout"
    assert payload["code"] == 124
    assert payload["type"] == "job.failed"
