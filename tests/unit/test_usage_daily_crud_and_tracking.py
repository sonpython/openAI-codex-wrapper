"""
Unit tests for usage_daily: CRUD upsert, middleware integration, worker path.

Covers:
  - UsageDaily model: field types and __repr__
  - upsert CRUD: creates row when none exists (mocked session)
  - upsert CRUD: accumulates on conflict (mocked session — two execute calls)
  - upsert CRUD: SQLite integration — insert + manual accumulate
  - UsageTrackingMiddleware: usage_daily_upsert called after successful response
  - UsageTrackingMiddleware: usage_daily_upsert NOT called on 4xx response
  - UsageTrackingMiddleware: usage_daily_upsert swallows exceptions (fail-open)
  - Worker success path: usage_daily_upsert called with job tokens
  - Worker success path: usage_daily_upsert skipped when api_key_id is None
  - admin_usage._query_daily_series: queries UsageDaily not Job
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-secret")

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.db.models import Base
from src.db.models_usage_daily import UsageDaily

# ── SQLite fixture ────────────────────────────────────────────────────────────


@pytest.fixture()
async def sqlite_session() -> AsyncGenerator[AsyncSession, None]:
    """In-memory SQLite session with all tables created (JSONB → JSON mapped)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    from sqlalchemy import JSON  # noqa: PLC0415

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(conn, _):  # type: ignore[misc]
        conn.execute("PRAGMA journal_mode=WAL")

    async with engine.begin() as conn:
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if hasattr(col.type, "__class__") and col.type.__class__.__name__ == "JSONB":
                    col.type = JSON()
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture()
async def seeded_ids(sqlite_session: AsyncSession) -> dict[str, uuid.UUID]:
    """Insert a User + ApiKey row and return their UUIDs."""
    user_id = uuid.uuid4()
    key_id = uuid.uuid4()

    await sqlite_session.execute(
        text("INSERT INTO users (id, email) VALUES (:uid, :email)"),
        {"uid": str(user_id), "email": f"test-{user_id}@example.com"},
    )
    await sqlite_session.execute(
        text(
            "INSERT INTO api_keys (id, user_id, key_hash, prefix, name, tier)"
            " VALUES (:kid, :uid, :kh, :pfx, :name, :tier)"
        ),
        {
            "kid": str(key_id),
            "uid": str(user_id),
            "kh": "hash",
            "pfx": "prefix123456",
            "name": "test-key",
            "tier": "free",
        },
    )
    await sqlite_session.commit()
    return {"user_id": user_id, "api_key_id": key_id}


# ── Model smoke tests ─────────────────────────────────────────────────────────


def test_usage_daily_repr() -> None:
    uid = uuid.uuid4()
    kid = uuid.uuid4()
    d = date(2026, 4, 29)
    row = UsageDaily(
        user_id=uid,
        api_key_id=kid,
        period=d,
        requests=5,
        input_tokens=100,
        output_tokens=50,
    )
    r = repr(row)
    assert "UsageDaily" in r
    assert str(uid) in r
    assert str(kid) in r


def test_usage_daily_tablename() -> None:
    assert UsageDaily.__tablename__ == "usage_daily"


def test_usage_daily_has_composite_pk_columns() -> None:
    """Verify composite PK columns are present on the table."""
    cols = {c.name for c in UsageDaily.__table__.primary_key.columns}
    assert cols == {"user_id", "api_key_id", "period"}


def test_usage_daily_has_expected_indexes() -> None:
    """Verify the two performance indexes are defined."""
    index_names = {idx.name for idx in UsageDaily.__table__.indexes}
    assert "ix_usage_daily_user_period" in index_names
    assert "ix_usage_daily_api_key_period" in index_names


# ── CRUD: mocked session (verify stmt shape) ──────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_executes_statement_once() -> None:
    """upsert() calls session.execute() exactly once and commits."""
    from src.db.crud.usage_daily import upsert

    mock_session = AsyncMock(spec=AsyncSession)
    uid = uuid.uuid4()
    kid = uuid.uuid4()

    await upsert(
        mock_session,
        user_id=uid,
        api_key_id=kid,
        period=date(2026, 4, 29),
        requests=1,
        input_tokens=100,
        output_tokens=50,
    )

    mock_session.execute.assert_awaited_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_called_twice_executes_twice() -> None:
    """Two upsert calls → two execute calls (accumulation logic lives in SQL)."""
    from src.db.crud.usage_daily import upsert

    mock_session = AsyncMock(spec=AsyncSession)
    uid = uuid.uuid4()
    kid = uuid.uuid4()
    period = date(2026, 4, 29)

    await upsert(mock_session, user_id=uid, api_key_id=kid, period=period, input_tokens=10)
    await upsert(mock_session, user_id=uid, api_key_id=kid, period=period, input_tokens=20)

    assert mock_session.execute.await_count == 2
    assert mock_session.commit.await_count == 2


# ── CRUD: SQLite integration (INSERT + SELECT roundtrip) ─────────────────────


@pytest.mark.asyncio
async def test_orm_insert_and_select_roundtrip(
    sqlite_session: AsyncSession, seeded_ids: dict[str, uuid.UUID]
) -> None:
    """Direct ORM INSERT; verify row is readable with correct values."""
    uid = seeded_ids["user_id"]
    kid = seeded_ids["api_key_id"]
    period = date(2026, 4, 29)

    row = UsageDaily(
        user_id=uid,
        api_key_id=kid,
        period=period,
        requests=1,
        input_tokens=100,
        output_tokens=50,
    )
    sqlite_session.add(row)
    await sqlite_session.commit()

    result = await sqlite_session.execute(
        select(UsageDaily).where(
            UsageDaily.user_id == uid,
            UsageDaily.api_key_id == kid,
            UsageDaily.period == period,
        )
    )
    fetched = result.scalar_one()
    assert fetched.requests == 1
    assert fetched.input_tokens == 100
    assert fetched.output_tokens == 50


@pytest.mark.asyncio
async def test_orm_accumulate_on_second_write(
    sqlite_session: AsyncSession, seeded_ids: dict[str, uuid.UUID]
) -> None:
    """ORM update simulating ON CONFLICT accumulation doubles the counts."""
    uid = seeded_ids["user_id"]
    kid = seeded_ids["api_key_id"]
    period = date(2026, 4, 28)

    row = UsageDaily(
        user_id=uid,
        api_key_id=kid,
        period=period,
        requests=1,
        input_tokens=50,
        output_tokens=25,
    )
    sqlite_session.add(row)
    await sqlite_session.commit()

    # Simulate ON CONFLICT DO UPDATE (additive) via ORM update
    result = await sqlite_session.execute(
        select(UsageDaily).where(
            UsageDaily.user_id == uid,
            UsageDaily.api_key_id == kid,
            UsageDaily.period == period,
        )
    )
    fetched = result.scalar_one()
    fetched.requests += 1
    fetched.input_tokens += 50
    fetched.output_tokens += 25
    await sqlite_session.commit()

    result2 = await sqlite_session.execute(
        select(UsageDaily).where(
            UsageDaily.user_id == uid,
            UsageDaily.api_key_id == kid,
            UsageDaily.period == period,
        )
    )
    final = result2.scalar_one()
    assert final.requests == 2
    assert final.input_tokens == 100
    assert final.output_tokens == 50


# ── Middleware integration ────────────────────────────────────────────────────


def _make_tracking_app(user_id: str, key_id: str, status: int = 200):  # type: ignore[no-untyped-def]
    """Build a minimal ASGI stack with pre-injected state → UsageTrackingMiddleware.

    State must be in scope BEFORE the middleware reads it (middleware checks
    user_id/api_key_id at the start of __call__, before delegating to inner app).
    We wrap the middleware in a thin outer ASGI layer that injects state first.
    """
    from src.gateway.middleware.usage_tracking import UsageTrackingMiddleware  # noqa: PLC0415
    from starlette.responses import Response as StarletteResponse  # noqa: PLC0415

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        resp = StarletteResponse(status_code=status)
        await resp(scope, receive, send)

    tracking = UsageTrackingMiddleware(inner)

    async def state_then_middleware(scope, receive, send):  # type: ignore[no-untyped-def]
        # Inject state before middleware reads it
        scope.setdefault("state", {}).update(
            {
                "user_id": user_id,
                "api_key_id": key_id,
                "usage": {
                    "total_tokens": 150,
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
                "tpm_estimated_cost": 150,
                "tpm_window_id": 12345,
            }
        )
        await tracking(scope, receive, send)

    return state_then_middleware


@pytest.mark.asyncio
async def test_middleware_calls_usage_daily_upsert_on_success() -> None:
    """Successful 2xx response → usage_daily_upsert called with correct args."""
    import asyncio  # noqa: PLC0415

    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    user_id = str(uuid.uuid4())
    key_id = str(uuid.uuid4())
    app = _make_tracking_app(user_id, key_id, status=200)

    upsert_calls: list[dict[str, Any]] = []

    async def _capture_upsert(session, **kwargs):  # type: ignore[no-untyped-def]
        upsert_calls.append(kwargs)

    mock_redis = AsyncMock()
    mock_redis.eval = AsyncMock(return_value=1)
    mock_redis.incrby = AsyncMock()
    mock_redis.expire = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.gateway.middleware.usage_tracking.get_settings") as mock_settings,
        patch("src.gateway.middleware.usage_tracking.get_client", return_value=mock_redis),
        patch("src.gateway.middleware.usage_tracking.bg_session", return_value=mock_cm),
        patch(
            "src.gateway.middleware.usage_tracking.usage_daily_upsert",
            new=AsyncMock(side_effect=_capture_upsert),
        ),
        patch("src.gateway.middleware.usage_tracking.usage_increment", new=AsyncMock()),
    ):
        settings_obj = MagicMock()
        settings_obj.rate_limit_bypass = False
        mock_settings.return_value = settings_obj

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/test")

        # Drain all pending bg tasks
        await asyncio.sleep(0.05)

    assert resp.status_code == 200
    assert len(upsert_calls) == 1
    assert upsert_calls[0]["input_tokens"] == 100
    assert upsert_calls[0]["output_tokens"] == 50
    assert upsert_calls[0]["requests"] == 1
    assert str(upsert_calls[0]["user_id"]) == user_id
    assert str(upsert_calls[0]["api_key_id"]) == key_id


@pytest.mark.asyncio
async def test_middleware_skips_daily_upsert_on_4xx() -> None:
    """4xx response → bg task never scheduled → usage_daily_upsert not called."""
    import asyncio  # noqa: PLC0415

    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    user_id = str(uuid.uuid4())
    key_id = str(uuid.uuid4())
    app = _make_tracking_app(user_id, key_id, status=400)

    mock_upsert = AsyncMock()

    with (
        patch("src.gateway.middleware.usage_tracking.get_settings") as mock_settings,
        patch("src.gateway.middleware.usage_tracking.get_client", return_value=AsyncMock()),
        patch("src.gateway.middleware.usage_tracking.usage_daily_upsert", mock_upsert),
    ):
        settings_obj = MagicMock()
        settings_obj.rate_limit_bypass = False
        mock_settings.return_value = settings_obj

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/fail")

        await asyncio.sleep(0.05)

    assert resp.status_code == 400
    mock_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_middleware_daily_upsert_exception_is_swallowed() -> None:
    """Exception in usage_daily_upsert is swallowed; response status unaffected."""
    import asyncio  # noqa: PLC0415

    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    user_id = str(uuid.uuid4())
    key_id = str(uuid.uuid4())
    app = _make_tracking_app(user_id, key_id, status=200)

    async def _exploding_upsert(session, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("DB down")

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.gateway.middleware.usage_tracking.get_settings") as mock_settings,
        patch("src.gateway.middleware.usage_tracking.get_client", return_value=AsyncMock()),
        patch("src.gateway.middleware.usage_tracking.bg_session", return_value=mock_cm),
        patch(
            "src.gateway.middleware.usage_tracking.usage_daily_upsert",
            new=AsyncMock(side_effect=_exploding_upsert),
        ),
        patch("src.gateway.middleware.usage_tracking.usage_increment", new=AsyncMock()),
    ):
        settings_obj = MagicMock()
        settings_obj.rate_limit_bypass = False
        mock_settings.return_value = settings_obj

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/ok")

        await asyncio.sleep(0.05)

    assert resp.status_code == 200


# ── Worker integration ────────────────────────────────────────────────────────


def _make_job_mock(
    api_key_id: uuid.UUID | None = None,
) -> MagicMock:
    """Build a minimal Job mock for worker tests."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = uuid.uuid4()
    job.api_key_id = api_key_id
    job.repo_url = "https://github.com/openai/codex"
    job.branch = "main"
    job.task = "Fix bug"
    job.mode = "read-only"
    return job


def _make_worker_context(cancel: bool = False) -> dict[str, Any]:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1" if cancel else None)
    return {"redis": redis}


@pytest.mark.asyncio
async def test_worker_success_calls_usage_daily_upsert() -> None:
    """Successful job with api_key_id → usage_daily_upsert called once with tokens."""
    import pathlib  # noqa: PLC0415

    from src.codex.events import TurnCompleted  # noqa: PLC0415
    from src.workers.git_diff import DiffResult  # noqa: PLC0415
    from src.workers.job_handlers import run_codex_job  # noqa: PLC0415

    key_id = uuid.uuid4()
    job = _make_job_mock(api_key_id=key_id)
    job_id = str(job.id)
    ctx = _make_worker_context()

    # TurnCompleted with no usage (tokens stay 0 — simpler; we test the call happens)
    async def _fake_codex(*a: Any, **kw: Any):  # type: ignore[return]
        tc = TurnCompleted(type="turn.completed")
        tc.usage = None
        yield tc

    diff_result = DiffResult(
        diff_blob="", diff_size_bytes=0, diff_truncated=False, files_changed=[]
    )
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    upsert_calls: list[dict[str, Any]] = []

    async def _capture(session: Any, **kwargs: Any) -> None:
        upsert_calls.append(kwargs)

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=job)),
        patch("src.workers.job_handlers.jobs_crud.mark_running", AsyncMock()),
        patch("src.workers.job_handlers.jobs_crud.mark_succeeded", AsyncMock()),
        patch("src.workers.job_handlers.jobs_crud.update_token_counts", AsyncMock()),
        patch("src.workers.job_handlers.publish_job_event", AsyncMock()),
        patch("src.workers.job_handlers.make_workspace", return_value=pathlib.Path("/tmp/ws-test")),
        patch("src.workers.job_handlers.cleanup_workspace", MagicMock()),
        patch("src.workers.job_handlers.git_clone", AsyncMock(return_value=(True, ""))),
        patch("src.workers.job_handlers.git_rev_parse_head", AsyncMock(return_value="abc")),
        patch("src.workers.job_handlers.capture_diff", AsyncMock(return_value=diff_result)),
        patch("src.workers.job_handlers.run_codex", _fake_codex),
        patch(
            "src.workers.job_handlers.usage_daily_upsert",
            new=AsyncMock(side_effect=_capture),
        ),
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "succeeded"
    assert len(upsert_calls) == 1
    assert upsert_calls[0]["user_id"] == job.user_id
    assert upsert_calls[0]["api_key_id"] == key_id
    assert upsert_calls[0]["requests"] == 1


@pytest.mark.asyncio
async def test_worker_skips_usage_daily_when_no_api_key_id() -> None:
    """Job with api_key_id=None → usage_daily_upsert not called."""
    import pathlib  # noqa: PLC0415

    from src.codex.events import TurnCompleted  # noqa: PLC0415
    from src.workers.git_diff import DiffResult  # noqa: PLC0415
    from src.workers.job_handlers import run_codex_job  # noqa: PLC0415

    job = _make_job_mock(api_key_id=None)
    job_id = str(job.id)
    ctx = _make_worker_context()

    async def _fake_codex(*a: Any, **kw: Any):  # type: ignore[return]
        tc = TurnCompleted(type="turn.completed")
        tc.usage = None
        yield tc

    diff_result = DiffResult(
        diff_blob="", diff_size_bytes=0, diff_truncated=False, files_changed=[]
    )
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    mock_upsert = AsyncMock()

    with (
        patch("src.workers.job_handlers.bg_session", return_value=mock_cm),
        patch("src.workers.job_handlers.jobs_crud.get_job_unscoped", AsyncMock(return_value=job)),
        patch("src.workers.job_handlers.jobs_crud.mark_running", AsyncMock()),
        patch("src.workers.job_handlers.jobs_crud.mark_succeeded", AsyncMock()),
        patch("src.workers.job_handlers.jobs_crud.update_token_counts", AsyncMock()),
        patch("src.workers.job_handlers.publish_job_event", AsyncMock()),
        patch(
            "src.workers.job_handlers.make_workspace", return_value=pathlib.Path("/tmp/ws-test2")
        ),
        patch("src.workers.job_handlers.cleanup_workspace", MagicMock()),
        patch("src.workers.job_handlers.git_clone", AsyncMock(return_value=(True, ""))),
        patch("src.workers.job_handlers.git_rev_parse_head", AsyncMock(return_value="abc")),
        patch("src.workers.job_handlers.capture_diff", AsyncMock(return_value=diff_result)),
        patch("src.workers.job_handlers.run_codex", _fake_codex),
        patch("src.workers.job_handlers.usage_daily_upsert", mock_upsert),
    ):
        result = await run_codex_job(ctx, job_id)

    assert result["status"] == "succeeded"
    mock_upsert.assert_not_awaited()


# ── admin_usage: query uses UsageDaily not Job ────────────────────────────────


@pytest.mark.asyncio
async def test_admin_usage_query_references_usage_daily_table() -> None:
    """_query_daily_series generates SQL referencing usage_daily, not jobs."""
    from src.gateway.routes.admin_usage import _query_daily_series  # noqa: PLC0415

    captured_stmts: list[Any] = []

    async def _mock_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        captured_stmts.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = _mock_execute  # type: ignore[method-assign]

    since = datetime(2026, 4, 22, tzinfo=UTC)
    rows = await _query_daily_series(mock_session, since)

    assert rows == []
    assert len(captured_stmts) == 1

    from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: PLC0415

    compiled = captured_stmts[0].compile(dialect=sqlite_dialect.dialect())
    sql_str = str(compiled)
    assert "usage_daily" in sql_str
    assert "jobs" not in sql_str


@pytest.mark.asyncio
async def test_admin_usage_query_filters_by_user_id() -> None:
    """user_id filter is applied to the query WHERE clause."""
    from src.gateway.routes.admin_usage import _query_daily_series  # noqa: PLC0415

    uid = uuid.uuid4()
    captured_stmts: list[Any] = []

    async def _mock_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        captured_stmts.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = _mock_execute  # type: ignore[method-assign]

    since = datetime(2026, 4, 22, tzinfo=UTC)
    await _query_daily_series(mock_session, since, user_id=uid)

    from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: PLC0415

    compiled = captured_stmts[0].compile(
        dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}
    )
    sql_str = str(compiled)
    # SQLite renders UUIDs without hyphens — check both forms
    uid_no_hyphens = str(uid).replace("-", "")
    assert uid_no_hyphens in sql_str or str(uid) in sql_str


@pytest.mark.asyncio
async def test_admin_usage_query_filters_by_api_key_id() -> None:
    """api_key_id filter is applied to the query WHERE clause."""
    from src.gateway.routes.admin_usage import _query_daily_series  # noqa: PLC0415

    kid = uuid.uuid4()
    captured_stmts: list[Any] = []

    async def _mock_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        captured_stmts.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = _mock_execute  # type: ignore[method-assign]

    since = datetime(2026, 4, 22, tzinfo=UTC)
    await _query_daily_series(mock_session, since, api_key_id=kid)

    from sqlalchemy.dialects import sqlite as sqlite_dialect  # noqa: PLC0415

    compiled = captured_stmts[0].compile(
        dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}
    )
    sql_str = str(compiled)
    kid_no_hyphens = str(kid).replace("-", "")
    assert kid_no_hyphens in sql_str or str(kid) in sql_str


@pytest.mark.asyncio
async def test_admin_usage_query_returns_daily_usage_rows() -> None:
    """Rows from usage_daily are mapped to DailyUsage schema correctly."""
    from src.gateway.routes.admin_usage import DailyUsage, _query_daily_series  # noqa: PLC0415

    fake_row = MagicMock()
    fake_row.day = date(2026, 4, 25)
    fake_row.requests = 7
    fake_row.tokens = 900

    async def _mock_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        result = MagicMock()
        result.all.return_value = [fake_row]
        return result

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = _mock_execute  # type: ignore[method-assign]

    since = datetime(2026, 4, 22, tzinfo=UTC)
    rows = await _query_daily_series(mock_session, since)

    assert len(rows) == 1
    assert isinstance(rows[0], DailyUsage)
    assert rows[0].day == "2026-04-25"
    assert rows[0].requests == 7
    assert rows[0].tokens == 900
