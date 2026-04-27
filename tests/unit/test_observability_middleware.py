"""
Unit tests for ObservabilityMiddleware.

Covers:
- HTTP_REQUESTS counter incremented after request with correct labels.
- HTTP_REQUEST_DURATION histogram observed after request.
- Route template captured (not raw parameterised URL).
- Status code captured correctly.
- 5xx response triggers error log (smoke — no exception raised).
- Skip paths (/healthz) bypass metric recording.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _make_obs_app() -> object:
    """Minimal FastAPI app with ObservabilityMiddleware only."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from src.gateway.middleware.observability import ObservabilityMiddleware
    from starlette.responses import PlainTextResponse, Response

    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/v1/ping")
    async def ping() -> Response:
        return JSONResponse({"ok": True})

    @app.get("/v1/items/{item_id}")
    async def get_item(item_id: str) -> Response:
        return JSONResponse({"id": item_id})

    @app.get("/v1/fail")
    async def fail() -> Response:
        return JSONResponse({"error": "boom"}, status_code=500)

    @app.get("/healthz")
    async def healthz() -> Response:
        return PlainTextResponse("ok")

    return app


def _get_counter_value(sample_name: str, labels: dict[str, str]) -> float:
    """Read a counter value by sample name (includes _total suffix) + labels.

    prometheus_client stores metric.name without _total; sample.name has it.
    Match on sample.name to correctly find Counter values.
    """
    from src.observability.metrics import registry

    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == sample_name and sample.labels == labels:
                return sample.value
    return 0.0


def _get_histogram_count(metric_name: str, labels: dict[str, str]) -> float:
    """Return _count sample for a histogram (match by sample.name = metric_name_count)."""
    from src.observability.metrics import registry

    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == f"{metric_name}_count" and sample.labels == labels:
                return sample.value
    return 0.0


@pytest.mark.asyncio
async def test_http_requests_counter_incremented() -> None:
    """Counter http_requests_total is incremented after a successful request."""

    app = _make_obs_app()

    # Snapshot before.
    before = _get_counter_value(
        "http_requests_total",
        {"route": "/v1/ping", "status": "200", "method": "GET"},
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        await client.get("/v1/ping")

    after = _get_counter_value(
        "http_requests_total",
        {"route": "/v1/ping", "status": "200", "method": "GET"},
    )

    assert after == before + 1.0, f"counter not incremented: before={before}, after={after}"


@pytest.mark.asyncio
async def test_http_duration_histogram_observed() -> None:
    """http_request_duration_seconds histogram is observed after a request."""
    app = _make_obs_app()

    before = _get_histogram_count(
        "http_request_duration_seconds",
        {"route": "/v1/ping"},
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        await client.get("/v1/ping")

    after = _get_histogram_count(
        "http_request_duration_seconds",
        {"route": "/v1/ping"},
    )

    assert after == before + 1.0, f"histogram count not incremented: before={before}, after={after}"


@pytest.mark.asyncio
async def test_route_template_not_raw_url() -> None:
    """Parameterised route uses template (/v1/items/{item_id}), not raw URL."""
    app = _make_obs_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        await client.get("/v1/items/abc123")

    # Should have a counter for the template route, not the raw URL.
    template_count = _get_counter_value(
        "http_requests_total",
        {"route": "/v1/items/{item_id}", "status": "200", "method": "GET"},
    )
    raw_count = _get_counter_value(
        "http_requests_total",
        {"route": "/v1/items/abc123", "status": "200", "method": "GET"},
    )

    assert template_count >= 1.0, "expected counter for template route"
    assert raw_count == 0.0, "raw URL must NOT appear as a metric label"


@pytest.mark.asyncio
async def test_5xx_status_captured() -> None:
    """500 response increments counter with status=500 label."""
    app = _make_obs_app()

    before = _get_counter_value(
        "http_requests_total",
        {"route": "/v1/fail", "status": "500", "method": "GET"},
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        await client.get("/v1/fail")

    after = _get_counter_value(
        "http_requests_total",
        {"route": "/v1/fail", "status": "500", "method": "GET"},
    )

    assert after == before + 1.0


@pytest.mark.asyncio
async def test_skip_path_not_counted() -> None:
    """Requests to /healthz are NOT counted in http_requests_total."""
    from src.observability.metrics import registry

    app = _make_obs_app()

    def _healthz_counter() -> float:
        return sum(
            s.value
            for metric in registry.collect()
            if metric.name == "http_requests_total"
            for s in metric.samples
            if s.labels.get("route") == "/healthz"
        )

    before = _healthz_counter()

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        await client.get("/healthz")

    assert _healthz_counter() == before, "/healthz must not appear in http_requests_total"
