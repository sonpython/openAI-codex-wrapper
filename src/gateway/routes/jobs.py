"""
/v1/codex/jobs routes: POST, GET, DELETE, GET /events (SSE).

Auth: AuthMiddleware sets request.state.user_id on all requests.

SSE contract (C3): headers passed via EventSourceResponse constructor,
not BaseHTTPMiddleware, to avoid Starlette response buffering.
keepalive_wrap (MM1) emits SSE comment keepalives every 15s of silence.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.db.crud import jobs as jobs_crud
from src.db.engine import main_session
from src.db.models import Job
from src.gateway.schemas.jobs import JobCreatedResponse, JobCreateRequest, JobResponse
from src.gateway.sse_helpers import keepalive_wrap
from src.redis_client import get_client
from src.settings import get_settings
from src.workers.event_publisher import TERMINAL_EVENT_TYPES
from src.workers.repo_url_head_check import RepoUrlCheckError, check_repo_url

logger = structlog.get_logger(__name__)
router = APIRouter()

_CANCEL_KEY_TTL = 300  # 5 min — matches worker poll budget


def _require_user_id(request: Request) -> uuid.UUID:
    """Extract user_id from request.state (set by AuthMiddleware)."""
    user_id: uuid.UUID | None = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user_id


async def _get_arq_pool() -> Any:
    """Return the Arq pool stored in app.state (initialised in lifespan)."""
    from src.gateway.app import _arq_pool  # noqa: PLC0415

    if _arq_pool is None:
        raise HTTPException(status_code=503, detail="Job queue not available.")
    return _arq_pool


# ── POST /v1/codex/jobs ───────────────────────────────────────────────────────


@router.post("/v1/codex/jobs", status_code=202)
async def create_job(
    request: Request,
    body: JobCreateRequest,
) -> JobCreatedResponse:
    """Enqueue a new codex job. Returns 202 with job id immediately."""
    user_id = _require_user_id(request)
    api_key_id: uuid.UUID | None = getattr(request.state, "api_key_id", None)
    settings = get_settings()
    redis = get_client()
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available.")

    # SSRF-hardened HEAD check before inserting into DB or touching Arq.
    # Schema already validated regex (github.com HTTPS only); this adds DNS
    # private-IP rejection + redirect rejection + response-code check.
    try:
        await check_repo_url(body.repo_url, redis)
    except RepoUrlCheckError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "invalid_request_error",
                    "message": str(exc.user_message),
                    "param": "repo_url",
                    "code": "invalid_request_error",
                }
            },
        ) from exc

    async with main_session() as session:
        job = await jobs_crud.create_job(
            session,
            user_id=user_id,
            api_key_id=api_key_id,
            repo_url=body.repo_url,
            branch=body.branch,
            task=body.task,
            mode=body.mode,
        )
        await session.commit()
        job_id = str(job.id)
        job_uuid = job.id
        enqueued_at = job.enqueued_at

    arq_pool = await _get_arq_pool()
    timeout = body.timeout_seconds or settings.job_default_timeout_seconds
    try:
        await arq_pool.enqueue_job("run_codex_job", job_id, _job_id=job_id)
    except Exception as exc:
        # H1: Arq enqueue failed — mark job failed so it doesn't orphan in queued state.
        logger.warning("job.enqueue_failed", job_id=job_id, error=str(exc))
        async with main_session() as session:
            await jobs_crud.mark_failed(
                session,
                job_uuid,
                error_code="enqueue_failed",
                error_message=str(exc)[:500],
            )
            await session.commit()
        raise HTTPException(status_code=503, detail="Job queue temporarily unavailable.") from exc

    await _publish_queued(redis, job_id, body, timeout)
    logger.info("job.enqueued", job_id=job_id, repo_url=body.repo_url)

    # C1: job creation itself carries no token cost (the codex tokens are
    # tracked out-of-band at job-completion time via the worker lifecycle).
    # Set usage to zero so UsageTrackingMiddleware skips its no-op correctly.
    request.state.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    return JobCreatedResponse(id=job.id, status="queued", created_at=enqueued_at)


async def _publish_queued(redis: Any, job_id: str, body: JobCreateRequest, timeout: int) -> None:
    from src.workers.event_publisher import publish_job_event  # noqa: PLC0415

    await publish_job_event(
        redis,
        job_id,
        "job.queued",
        {
            "repo_url": body.repo_url,
            "branch": body.branch,
            "mode": body.mode,
            "timeout_seconds": timeout,
        },
    )


# ── GET /v1/codex/jobs/{job_id} ───────────────────────────────────────────────


@router.get("/v1/codex/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> JobResponse:
    """Return full job state. 404 if not found or not owned by caller."""
    user_id = _require_user_id(request)

    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found.") from None

    async with main_session() as session:
        job: Job | None = await jobs_crud.get_job(session, jid, user_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    return JobResponse.from_job(job)


# ── DELETE /v1/codex/jobs/{job_id} ────────────────────────────────────────────


@router.delete("/v1/codex/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str) -> JobResponse:
    """Cancel a job. Idempotent — always returns 200 with current state."""
    user_id = _require_user_id(request)
    redis = get_client()
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available.")

    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found.") from None

    async with main_session() as session:
        job: Job | None = await jobs_crud.get_job(session, jid, user_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        status = job.status
        if status == "queued":
            # H2: Set cancel flag — do NOT publish job.cancelled here.
            # Worker's early-cancel branch handles the single terminal publication.
            await redis.set(f"cancel:job:{job_id}", "1", ex=_CANCEL_KEY_TTL)
            # H3: Guard UPDATE with status='queued' WHERE clause (mark_cancelled checks status).
            rows_updated = await jobs_crud.mark_cancelled(session, jid, guard_status="queued")
            if rows_updated:
                await session.commit()
        elif status == "running":
            # Signal worker via Redis flag; worker transitions state.
            await redis.set(f"cancel:job:{job_id}", "1", ex=_CANCEL_KEY_TTL)
        # Terminal states: no-op (idempotent).

        # Re-fetch for fresh state
        job = await jobs_crud.get_job(session, jid, user_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobResponse.from_job(job)


# ── GET /v1/codex/jobs/{job_id}/events (SSE) ─────────────────────────────────


@router.get("/v1/codex/jobs/{job_id}/events")
async def stream_job_events(request: Request, job_id: str) -> StreamingResponse:
    """Stream job lifecycle events via SSE.

    Replays buffered events from Redis list, then subscribes to live channel.
    Closes automatically when a terminal event (job.completed/failed/cancelled)
    is received.

    keepalive_wrap emits SSE comment lines every 15s of silence (MM1).
    Headers set in StreamingResponse constructor (C3 — no BaseHTTPMiddleware).
    """
    user_id = _require_user_id(request)

    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found.") from None

    redis = get_client()
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available.")

    async with main_session() as session:
        job: Job | None = await jobs_crud.get_job(session, jid, user_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    # Phase-06 rate-limit headers (safe default — phase 06 populates this).
    rate_limit_headers: dict[str, str] = getattr(request.state, "rate_limit_headers", {})

    inner = _replay_then_subscribe(job_id, request, redis)
    wrapped = keepalive_wrap(inner, interval=15.0)

    return StreamingResponse(
        wrapped,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            **rate_limit_headers,
        },
    )


async def _replay_then_subscribe(
    job_id: str,
    request: Request,
    redis: Any,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE bytes: first replay buffered events, then subscribe live."""
    list_key = f"job:events:list:{job_id}"
    channel_key = f"job:events:{job_id}"

    # ── Replay buffered events ────────────────────────────────────────────
    backlog: list[bytes] = await redis.lrange(list_key, 0, -1)
    for raw in backlog:
        if await request.is_disconnected():
            return
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        data = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
        yield f"event: {evt.get('type', 'message')}\ndata: {data}\n\n".encode()
        if evt.get("type") in TERMINAL_EVENT_TYPES:
            return

    # ── Live subscription ─────────────────────────────────────────────────
    # C2: Poll with timeout=1.0 instead of pubsub.listen() to avoid hanging
    # forever on Redis disconnect. get_message() yields None on idle, letting
    # keepalive_wrap emit comment bytes and allowing disconnect checks.
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(channel_key)
        while not await request.is_disconnected():
            msg = await pubsub.get_message(timeout=1.0, ignore_subscribe_messages=True)
            if msg is None:
                continue  # idle tick — keepalive_wrap handles comment emission
            if msg.get("type") != "message":
                continue
            raw_data = msg.get("data", b"")
            try:
                evt = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                continue
            data = (
                raw_data
                if isinstance(raw_data, str)
                else raw_data.decode("utf-8", errors="replace")
            )
            yield f"event: {evt.get('type', 'message')}\ndata: {data}\n\n".encode()
            if evt.get("type") in TERMINAL_EVENT_TYPES:
                break
    finally:
        try:
            await pubsub.unsubscribe(channel_key)
            await pubsub.aclose()
        except Exception as close_exc:
            logger.warning("sse.pubsub_close_error", error=str(close_exc))
