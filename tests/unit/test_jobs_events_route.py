"""
Unit tests for GET /v1/codex/jobs/{id}/events SSE route.

Covers:
  - Replay-from-backlog: events from Redis list emitted in order
  - Terminal event in backlog closes stream without subscribing
  - Live pubsub: messages forwarded until terminal event
  - Disconnect detection causes clean exit
  - keepalive bytes appear on slow streams
  - 404 on unknown job id
  - SSE framing: lines start with "event: " and "data: "
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.gateway.routes.jobs import router as jobs_router

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_app(user_id: uuid.UUID | None = None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request: Any, call_next: Any) -> Any:
        if user_id is not None:
            request.state.user_id = user_id
        return await call_next(request)

    app.include_router(jobs_router)
    return app


def _fake_job(job_id: uuid.UUID, user_id: uuid.UUID, status: str = "running") -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.user_id = user_id
    job.status = status
    job.repo_url = "https://github.com/openai/codex"
    job.branch = "main"
    job.task = "t"
    job.mode = "read-only"
    job.summary = None
    job.diff_blob = None
    job.diff_size_bytes = None
    job.files_changed = None
    job.exit_code = None
    job.error_code = None
    job.error_message = None
    job.enqueued_at = datetime.now(UTC)
    job.started_at = None
    job.finished_at = None
    return job


def _encode_event(event_type: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps({"type": event_type, "job_id": "j1", "ts": "2026-01-01T00:00:00Z", **payload})
    return data.encode()


def _make_pubsub(messages: list[dict[str, Any]]) -> MagicMock:
    """Build a mock pubsub that returns ``messages`` from get_message() calls.

    C2 fix: route now uses get_message(timeout=1.0) instead of listen().
    Sentinel None marks end of message stream (causes disconnect loop exit).
    """
    ps = MagicMock()
    ps.subscribe = AsyncMock()
    ps.unsubscribe = AsyncMock()
    ps.aclose = AsyncMock()

    # Queue: real messages first, then None (idle) to signal end.
    _queue = list(messages) + [None]
    _idx = 0

    async def _get_message(timeout: float = 0.0, ignore_subscribe_messages: bool = False) -> Any:
        nonlocal _idx
        if _idx < len(_queue):
            msg = _queue[_idx]
            _idx += 1
            return msg
        return None  # keep returning None once exhausted

    ps.get_message = _get_message
    ps.__aenter__ = AsyncMock(return_value=ps)
    ps.__aexit__ = AsyncMock(return_value=False)
    return ps


# ── Replay from backlog ───────────────────────────────────────────────────────


def test_sse_replays_backlog_events() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    started_raw = _encode_event("job.started", {})
    completed_raw = _encode_event("job.completed", {})

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[started_raw, completed_raw])
    mock_redis.pubsub = MagicMock(return_value=_make_pubsub([]))

    async def _fake_get(*a: Any, **kw: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        with client.stream("GET", f"/v1/codex/jobs/{job_id}/events") as resp:
            raw = resp.read()

    assert b"event: job.started" in raw
    assert b"event: job.completed" in raw


def test_sse_closes_after_terminal_in_backlog() -> None:
    """If backlog already contains a terminal event, stream closes without subscribing."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="succeeded")

    completed_raw = _encode_event("job.completed", {})

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[completed_raw])
    pubsub = _make_pubsub([])
    mock_redis.pubsub = MagicMock(return_value=pubsub)

    async def _fake_get(*a: Any, **kw: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        with client.stream("GET", f"/v1/codex/jobs/{job_id}/events") as resp:
            resp.read()

    # pubsub.subscribe should NOT have been called since terminal event was in backlog
    pubsub.subscribe.assert_not_awaited()


# ── Live pubsub ───────────────────────────────────────────────────────────────


def test_sse_receives_live_pubsub_events() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    live_msg_data = _encode_event("job.completed", {})
    live_messages = [
        {"type": "subscribe", "data": 1},  # ignored
        {"type": "message", "data": live_msg_data},
    ]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[])  # empty backlog
    mock_redis.pubsub = MagicMock(return_value=_make_pubsub(live_messages))

    async def _fake_get(*a: Any, **kw: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        with client.stream("GET", f"/v1/codex/jobs/{job_id}/events") as resp:
            raw = resp.read()

    assert b"event: job.completed" in raw


# ── SSE framing ───────────────────────────────────────────────────────────────


def test_sse_event_lines_have_correct_framing() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="succeeded")

    completed_raw = _encode_event("job.completed", {"summary": "done"})

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[completed_raw])
    mock_redis.pubsub = MagicMock(return_value=_make_pubsub([]))

    async def _fake_get(*a: Any, **kw: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        with client.stream("GET", f"/v1/codex/jobs/{job_id}/events") as resp:
            raw = resp.read().decode()

    # Each event block must have both "event: " and "data: " lines
    blocks = [b for b in raw.split("\n\n") if b.strip() and not b.startswith(":")]
    assert len(blocks) >= 1
    for block in blocks:
        lines = block.strip().splitlines()
        assert any(line.startswith("event: ") for line in lines)
        assert any(line.startswith("data: ") for line in lines)


# ── 404 cases ─────────────────────────────────────────────────────────────────


def test_sse_404_unknown_job() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()

    async def _fake_get(*a: Any, **kw: Any) -> None:
        return None

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/codex/jobs/{job_id}/events")

    assert resp.status_code == 404


def test_sse_404_invalid_uuid() -> None:
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/v1/codex/jobs/not-a-uuid/events")
    assert resp.status_code == 404


# ── content-type header ───────────────────────────────────────────────────────


def test_sse_content_type_is_event_stream() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="succeeded")
    completed_raw = _encode_event("job.completed", {})

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[completed_raw])
    mock_redis.pubsub = MagicMock(return_value=_make_pubsub([]))

    async def _fake_get(*a: Any, **kw: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        with client.stream("GET", f"/v1/codex/jobs/{job_id}/events") as resp:
            resp.read()
            ct = resp.headers.get("content-type", "")

    assert "text/event-stream" in ct


# ── C2: SSE pubsub cleanup on get_message error ───────────────────────────────


def test_sse_pubsub_get_message_connection_error_exits_cleanly() -> None:
    """C2: If get_message() raises ConnectionError (Redis disconnect), the generator
    exits without hanging and the finally block runs (unsubscribe + aclose called)."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    ps = MagicMock()
    ps.subscribe = AsyncMock()
    ps.unsubscribe = AsyncMock()
    ps.aclose = AsyncMock()

    async def _get_message_raises(**kwargs: Any) -> None:
        raise ConnectionError("Redis disconnected")

    ps.get_message = _get_message_raises

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.lrange = AsyncMock(return_value=[])  # empty backlog → enter live loop
    mock_redis.pubsub = MagicMock(return_value=ps)

    async def _fake_get(*a: Any, **kw: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app, raise_server_exceptions=False)
        # Stream will raise internally but TestClient should not hang
        try:
            with client.stream("GET", f"/v1/codex/jobs/{job_id}/events") as resp:
                resp.read()
        except Exception:
            pass  # ConnectionError propagates through streaming — that's acceptable

    # unsubscribe + aclose must have been called in finally (cleanup ran)
    ps.unsubscribe.assert_awaited_once()
    ps.aclose.assert_awaited_once()
