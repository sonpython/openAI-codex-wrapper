"""
Job lifecycle event publisher.

Each published event is:
  1. RPUSH'd to ``job:events:list:{job_id}`` — replay buffer for late SSE subscribers.
  2. EXPIRE'd with 24h TTL on the list (reset on each publish to extend window).
  3. PUBLISH'd to ``job:events:{job_id}`` — live pub/sub channel for active subscribers.

Pipeline batches all three Redis commands in one round-trip.

Terminal event types: job.completed | job.failed | job.cancelled
SSE /events route exits its subscribe loop when it sees a terminal type.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

# SSE /events route exits on seeing any of these event types.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"job.completed", "job.failed", "job.cancelled"})

_LIST_TTL_SECONDS = 86_400  # 24 hours


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def publish_job_event(
    redis: Redis[Any],
    job_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Publish a job lifecycle event to Redis replay buffer + live channel.

    Args:
        redis:      Shared Redis client (redis.asyncio.Redis).
        job_id:     String UUID of the job.
        event_type: Event type string (e.g. "job.started", "job.codex_event").
        payload:    Arbitrary dict merged into the event body.
    """
    envelope: dict[str, Any] = {
        "type": event_type,
        "job_id": job_id,
        "ts": _now_iso(),
        **payload,
    }
    raw = json.dumps(envelope)
    list_key = f"job:events:list:{job_id}"
    channel_key = f"job:events:{job_id}"

    async with redis.pipeline(transaction=False) as pipe:
        pipe.rpush(list_key, raw)
        pipe.expire(list_key, _LIST_TTL_SECONDS)
        pipe.publish(channel_key, raw)
        await pipe.execute()
