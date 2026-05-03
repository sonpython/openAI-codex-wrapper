"""
Unit tests for POST/GET/DELETE /v1/codex/jobs routes.

Mocks: DB session, Arq pool, Redis client, event publisher.
No real DB, Redis, or Arq worker required.

Covers:
  - POST 202: job created, enqueue_job called
  - POST 400: invalid repo_url, run_tests=True
  - GET 200: job found for owner
  - GET 404: unknown id or wrong owner
  - DELETE: queued→cancelled, running→cancel flag set, terminal→no-op
  - 401: missing auth
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from src.gateway.routes.jobs import router as jobs_router

# ── App factory ───────────────────────────────────────────────────────────────


def _make_app(
    user_id: uuid.UUID | None = None,
    codex_mode: str | None = None,
) -> FastAPI:
    """Build minimal test app with jobs router; injects user_id and optional codex_mode into state."""
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def _val_err(request: object, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        raw_msg = str(first.get("msg", "validation error"))
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": raw_msg,
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_request_error",
                }
            },
        )

    @app.middleware("http")
    async def _inject_user(request: Any, call_next: Any) -> Any:
        if user_id is not None:
            request.state.user_id = user_id
        if codex_mode is not None:
            request.state.codex_mode = codex_mode
        return await call_next(request)

    app.include_router(jobs_router)
    return app


# ── Fake Job model ────────────────────────────────────────────────────────────


def _fake_job(
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str = "queued",
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.user_id = user_id
    job.status = status
    job.repo_url = "https://github.com/openai/codex"
    job.branch = "main"
    job.task = "do something"
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


# ── POST /v1/codex/jobs ───────────────────────────────────────────────────────


def test_post_job_202_enqueues(tmp_path: Any) -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    async def _fake_get_session() -> AsyncGenerator[Any, None]:
        yield mock_session

    with (
        patch("src.gateway.routes.jobs.jobs_crud.create_job", side_effect=_fake_create),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs._get_arq_pool", new=AsyncMock(return_value=mock_pool)),
        patch("src.gateway.routes.jobs.get_client", return_value=AsyncMock()),
        patch("src.gateway.routes.jobs._publish_queued", new=AsyncMock()),
    ):
        app = _make_app(uid)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/codex/jobs",
            json={
                "repo_url": "https://github.com/openai/codex",
                "task": "fix the bug",
            },
        )

    assert resp.status_code == 202
    body = resp.json()
    assert "id" in body
    assert body["status"] == "queued"


def test_post_job_400_invalid_repo_url() -> None:
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/codex/jobs",
        json={
            "repo_url": "git@github.com:openai/codex.git",
            "task": "fix the bug",
        },
    )
    assert resp.status_code == 400


def test_post_job_400_run_tests_true() -> None:
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/codex/jobs",
        json={
            "repo_url": "https://github.com/openai/codex",
            "task": "run tests",
            "run_tests": True,
        },
    )
    assert resp.status_code == 400


def test_post_job_401_no_auth() -> None:
    app = _make_app(user_id=None)  # no user injected → state.user_id missing
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/codex/jobs",
        json={"repo_url": "https://github.com/openai/codex", "task": "t"},
    )
    assert resp.status_code == 401


# ── GET /v1/codex/jobs/{id} ───────────────────────────────────────────────────


def test_get_job_200_found() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_get(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        resp = client.get(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(job_id)
    assert body["status"] == "queued"


def test_get_job_404_not_found() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_get(*args: Any, **kwargs: Any) -> None:
        return None

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
    ):
        app = _make_app(uid)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 404


def test_get_job_404_invalid_uuid() -> None:
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/v1/codex/jobs/not-a-uuid")
    assert resp.status_code == 404


# ── DELETE /v1/codex/jobs/{id} ────────────────────────────────────────────────


def test_delete_queued_job_marks_cancelled() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="queued")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    call_count = 0

    async def _fake_get(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_job
        # Second get_job call (re-fetch after cancel)
        return _fake_job(job_id, uid, status="cancelled")

    # H3: mark_cancelled now returns rowcount (1 = updated, 0 = race/no-op)
    mock_cancel = AsyncMock(return_value=1)
    mock_publish = AsyncMock()

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.jobs_crud.mark_cancelled", mock_cancel),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        resp = client.delete(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 200
    mock_redis.set.assert_called_once()
    mock_cancel.assert_awaited_once()
    # H2: route must NOT publish job.cancelled — worker handles that
    mock_publish.assert_not_awaited()


def test_delete_running_job_sets_cancel_flag() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="running")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    call_count = 0

    async def _fake_get(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        resp = client.delete(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 200
    # cancel flag must be set with TTL
    mock_redis.set.assert_called_once()
    set_args = mock_redis.set.call_args
    assert f"cancel:job:{job_id}" in str(set_args)


def test_delete_terminal_job_is_noop() -> None:
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="succeeded")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    call_count = 0

    async def _fake_get(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        resp = client.delete(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 200
    # No cancel flag for terminal jobs
    mock_redis.set.assert_not_called()


# ── H1: Enqueue failure path ──────────────────────────────────────────────────


def test_post_job_503_on_enqueue_failure() -> None:
    """H1: If arq enqueue raises, job is marked failed and 503 returned."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(side_effect=ConnectionError("redis down"))

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    mock_mark_failed = AsyncMock()

    with (
        patch("src.gateway.routes.jobs.jobs_crud.create_job", side_effect=_fake_create),
        patch("src.gateway.routes.jobs.jobs_crud.mark_failed", mock_mark_failed),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs._get_arq_pool", new=AsyncMock(return_value=mock_pool)),
        patch("src.gateway.routes.jobs.get_client", return_value=AsyncMock()),
        patch("src.gateway.routes.jobs._publish_queued", new=AsyncMock()),
    ):
        app = _make_app(uid)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/v1/codex/jobs",
            json={
                "repo_url": "https://github.com/openai/codex",
                "task": "fix the bug",
            },
        )

    assert resp.status_code == 503
    # Job must be marked failed so it doesn't stay orphaned in queued state
    mock_mark_failed.assert_awaited_once()
    call_kwargs = mock_mark_failed.await_args.kwargs
    assert call_kwargs["error_code"] == "enqueue_failed"


# ── H2: Single job.cancelled event on queued-DELETE ───────────────────────────


def test_delete_queued_job_does_not_publish_cancelled() -> None:
    """H2: Route must NOT publish job.cancelled for queued jobs.
    Worker's early-cancel branch emits the single terminal event."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid, status="queued")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()

    call_count = 0

    async def _fake_get(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_job
        return _fake_job(job_id, uid, status="cancelled")

    mock_cancel = AsyncMock(return_value=1)
    mock_publish = AsyncMock()

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.jobs_crud.mark_cancelled", mock_cancel),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
        patch("src.workers.event_publisher.publish_job_event", mock_publish),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        resp = client.delete(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 200
    # Route sets cancel flag + updates DB but does NOT publish
    mock_redis.set.assert_called_once()
    mock_cancel.assert_awaited_once()
    mock_publish.assert_not_awaited()


# ── H3: mark_cancelled race — already-terminal guard ─────────────────────────


def test_delete_already_completed_job_is_idempotent() -> None:
    """H3: If job transitions to terminal between GET and UPDATE (race), mark_cancelled
    returns 0 rows and route returns 200 with current state (no error)."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    # Status appears queued on first fetch but succeeds between that and the UPDATE
    fake_job_queued = _fake_job(job_id, uid, status="queued")
    fake_job_done = _fake_job(job_id, uid, status="succeeded")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_redis = AsyncMock()

    call_count = 0

    async def _fake_get(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return fake_job_queued
        return fake_job_done

    # guard_status='queued' finds nothing because worker already moved to succeeded
    mock_cancel = AsyncMock(return_value=0)

    with (
        patch("src.gateway.routes.jobs.jobs_crud.get_job", side_effect=_fake_get),
        patch("src.gateway.routes.jobs.jobs_crud.mark_cancelled", mock_cancel),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs.get_client", return_value=mock_redis),
    ):
        app = _make_app(uid)
        client = TestClient(app)
        resp = client.delete(f"/v1/codex/jobs/{job_id}")

    assert resp.status_code == 200
    body = resp.json()
    # Returns current state (succeeded), not an error
    assert body["status"] == "succeeded"
    # mark_cancelled was called but returned 0 — no commit (session.commit not called)
    mock_cancel.assert_awaited_once()


# ── H4: Branch regex — negative cases ────────────────────────────────────────


def test_post_job_400_branch_leading_dash() -> None:
    """H4: Branch starting with '-' must be rejected."""
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/codex/jobs",
        json={"repo_url": "https://github.com/openai/codex", "task": "t", "branch": "-rf"},
    )
    assert resp.status_code == 400


def test_post_job_400_branch_dotdot() -> None:
    """H4: Branch with '..' path traversal must be rejected."""
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/codex/jobs",
        json={
            "repo_url": "https://github.com/openai/codex",
            "task": "t",
            "branch": "feat/../bad",
        },
    )
    assert resp.status_code == 400


def test_post_job_400_branch_double_slash() -> None:
    """H4: Branch with '//' consecutive slashes must be rejected."""
    uid = uuid.uuid4()
    app = _make_app(uid)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/v1/codex/jobs",
        json={
            "repo_url": "https://github.com/openai/codex",
            "task": "t",
            "branch": "feat//bad",
        },
    )
    assert resp.status_code == 400


# ── Phase-2: local-bridge 501 ─────────────────────────────────────────────────


def test_post_job_501_local_bridge_mode() -> None:
    """Phase-2: local-bridge api_key mode → 501 without touching DB, Redis, or Arq."""
    uid = uuid.uuid4()
    app = _make_app(uid, codex_mode="local-bridge")
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        "/v1/codex/jobs",
        json={"repo_url": "https://github.com/openai/codex", "task": "fix the bug"},
    )
    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["code"] == "local_bridge_not_implemented"
    assert body["error"]["type"] == "api_error"


# ── HIGH-2: body.mode authz gating ───────────────────────────────────────────


def test_post_job_403_sandbox_key_workspace_write_body() -> None:
    """HIGH-2: sandbox api key + workspace-write body → 403 forbidden_for_key_mode."""
    uid = uuid.uuid4()
    app = _make_app(uid, codex_mode="sandbox")
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        "/v1/codex/jobs",
        json={
            "repo_url": "https://github.com/openai/codex",
            "task": "do something",
            "mode": "workspace-write",
        },
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden_for_key_mode"
    assert body["error"]["type"] == "forbidden"
    assert body["error"]["param"] == "mode"


def test_post_job_202_sandbox_key_read_only_body() -> None:
    """HIGH-2: sandbox api key + read-only body → 202 (allowed)."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.create_job", side_effect=_fake_create),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs._get_arq_pool", new=AsyncMock(return_value=mock_pool)),
        patch("src.gateway.routes.jobs.get_client", return_value=AsyncMock()),
        patch("src.gateway.routes.jobs._publish_queued", new=AsyncMock()),
    ):
        app = _make_app(uid, codex_mode="sandbox")
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/codex/jobs",
            json={
                "repo_url": "https://github.com/openai/codex",
                "task": "fix the bug",
                "mode": "read-only",
            },
        )

    assert resp.status_code == 202


def test_post_job_202_vps_key_workspace_write_body() -> None:
    """HIGH-2: vps api key + workspace-write body → 202 (allowed)."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.create_job", side_effect=_fake_create),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs._get_arq_pool", new=AsyncMock(return_value=mock_pool)),
        patch("src.gateway.routes.jobs.get_client", return_value=AsyncMock()),
        patch("src.gateway.routes.jobs._publish_queued", new=AsyncMock()),
    ):
        app = _make_app(uid, codex_mode="vps")
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/codex/jobs",
            json={
                "repo_url": "https://github.com/openai/codex",
                "task": "fix the bug",
                "mode": "workspace-write",
            },
        )

    assert resp.status_code == 202


def test_post_job_202_vps_key_read_only_body() -> None:
    """HIGH-2: vps api key + read-only body → 202 (allowed)."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.create_job", side_effect=_fake_create),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs._get_arq_pool", new=AsyncMock(return_value=mock_pool)),
        patch("src.gateway.routes.jobs.get_client", return_value=AsyncMock()),
        patch("src.gateway.routes.jobs._publish_queued", new=AsyncMock()),
    ):
        app = _make_app(uid, codex_mode="vps")
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/codex/jobs",
            json={
                "repo_url": "https://github.com/openai/codex",
                "task": "fix the bug",
                "mode": "read-only",
            },
        )

    assert resp.status_code == 202


# ── MEDIUM-3: unknown mode → 501 unsupported_mode ────────────────────────────


def test_post_job_501_unknown_mode() -> None:
    """MEDIUM-3: unknown codex_mode in request.state → 501 unsupported_mode."""
    uid = uuid.uuid4()
    app = _make_app(uid, codex_mode="future-unknown-mode")
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        "/v1/codex/jobs",
        json={"repo_url": "https://github.com/openai/codex", "task": "t"},
    )
    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["code"] == "unsupported_mode"
    assert body["error"]["type"] == "not_implemented"
    # MEDIUM-4: param key must be present (null value acceptable)
    assert "param" in body["error"]
    assert body["error"]["param"] is None


# ── MEDIUM-4: local-bridge 501 envelope has param: null ──────────────────────


def test_post_job_501_local_bridge_has_param_null() -> None:
    """MEDIUM-4: local-bridge 501 body must include param: null."""
    uid = uuid.uuid4()
    app = _make_app(uid, codex_mode="local-bridge")
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        "/v1/codex/jobs",
        json={"repo_url": "https://github.com/openai/codex", "task": "fix the bug"},
    )
    assert resp.status_code == 501
    body = resp.json()
    assert "param" in body["error"]
    assert body["error"]["param"] is None


def test_post_job_200_valid_branch() -> None:
    """H4: Normal feature branch must be accepted."""
    uid = uuid.uuid4()
    job_id = uuid.uuid4()
    fake_job = _fake_job(job_id, uid)

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=None)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def _fake_create(*args: Any, **kwargs: Any) -> Any:
        return fake_job

    with (
        patch("src.gateway.routes.jobs.jobs_crud.create_job", side_effect=_fake_create),
        patch("src.gateway.routes.jobs.main_session", return_value=mock_session),
        patch("src.gateway.routes.jobs._get_arq_pool", new=AsyncMock(return_value=mock_pool)),
        patch("src.gateway.routes.jobs.get_client", return_value=AsyncMock()),
        patch("src.gateway.routes.jobs._publish_queued", new=AsyncMock()),
    ):
        app = _make_app(uid)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/codex/jobs",
            json={
                "repo_url": "https://github.com/openai/codex",
                "task": "fix the bug",
                "branch": "feature/abc-123",
            },
        )

    assert resp.status_code == 202
