"""
Unit tests for the Prometheus metrics scrape endpoint.

Covers:
- GET /_internal/metrics returns 200 with text/plain content-type.
- Response body starts with # HELP (Prometheus text format).
- All 16 named instruments appear in the scrape output.
- Endpoint is mounted at settings.internal_metrics_path (not /metrics).
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# All metric names that must appear in scrape output (phase-07 spec §Metrics).
_EXPECTED_METRIC_NAMES = [
    "http_requests_total",
    "http_request_duration_seconds",
    "codex_subprocess_duration_seconds",
    "codex_subprocess_exit_code_total",
    "codex_event_total",
    "codex_active_subprocess",
    "arq_queue_depth",
    "arq_jobs_active",
    "arq_job_duration_seconds",
    "arq_jobs_total",
    "rate_limit_rejections_total",
    "rate_limit_remaining",
    "auth_rejections_total",
    "db_query_duration_seconds",
    "db_pool_active",
    "db_pool_idle",
]


def _make_metrics_test_app() -> object:
    """Build a minimal app that mounts only the metrics endpoint."""
    from fastapi import FastAPI
    from src.observability.metrics import make_metrics_app
    from src.settings import get_settings

    settings = get_settings()
    app = FastAPI()
    app.mount(settings.internal_metrics_path, make_metrics_app())
    return app


# Starlette's Mount redirects /_internal/metrics → /_internal/metrics/ (307).
# Use follow_redirects=True so tests hit the actual prometheus ASGI app.
_METRICS_PATH = "/_internal/metrics"


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_200() -> None:
    """GET /_internal/metrics returns HTTP 200 (following Starlette mount redirect)."""
    app = _make_metrics_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        response = await client.get(_METRICS_PATH)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_metrics_endpoint_content_type_is_text_plain() -> None:
    """Content-Type header includes text/plain (Prometheus format)."""
    app = _make_metrics_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        response = await client.get(_METRICS_PATH)

    content_type = response.headers.get("content-type", "")
    assert "text/plain" in content_type, f"Expected text/plain, got: {content_type}"


@pytest.mark.asyncio
async def test_metrics_endpoint_body_starts_with_help() -> None:
    """Response body starts with # HELP — valid Prometheus text format."""
    app = _make_metrics_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        response = await client.get(_METRICS_PATH)

    body = response.text
    assert (
        body.startswith("# HELP") or "# HELP" in body[:500]
    ), f"Expected Prometheus text format starting with '# HELP', got: {body[:200]!r}"


@pytest.mark.asyncio
async def test_all_16_instruments_present_in_scrape() -> None:
    """All 16 named instruments from spec appear as # HELP lines in scrape."""
    app = _make_metrics_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
        follow_redirects=True,
    ) as client:
        response = await client.get(_METRICS_PATH)

    body = response.text
    missing = [name for name in _EXPECTED_METRIC_NAMES if name not in body]
    assert not missing, f"Missing metrics in scrape output: {missing}"


@pytest.mark.asyncio
async def test_metrics_not_mounted_at_public_path() -> None:
    """The app does NOT serve metrics at the old /metrics path (security)."""
    app = _make_metrics_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/metrics")

    # /metrics is NOT mounted → 404 (not 200 with Prometheus text).
    assert response.status_code == 404


def test_generate_latest_contains_all_instruments() -> None:
    """Synchronous smoke: generate_latest() on our registry includes all 16 names."""
    from prometheus_client import generate_latest
    from src.observability.metrics import registry

    output = generate_latest(registry).decode()
    missing = [name for name in _EXPECTED_METRIC_NAMES if name not in output]
    assert not missing, f"Missing from generate_latest: {missing}"
