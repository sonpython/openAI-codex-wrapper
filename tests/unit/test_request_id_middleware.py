"""
Unit tests for RequestIDMiddleware.

Covers:
- Generates X-Request-Id when header absent.
- Propagates X-Request-Id header when client provides one.
- Response always echoes X-Request-Id header back.
- structlog contextvars binding visible to a logger called within request scope.
- Skip paths (/healthz, /_internal/metrics) pass through without modification.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _make_test_app() -> object:
    """Minimal FastAPI app with only RequestIDMiddleware."""
    import structlog
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from src.gateway.middleware.request_id import RequestIDMiddleware
    from starlette.responses import Response

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/v1/test")
    async def handler() -> Response:
        ctx = structlog.contextvars.get_contextvars()
        return JSONResponse({"request_id_in_ctx": ctx.get("request_id", "")})

    return app


@pytest.mark.asyncio
async def test_generates_request_id_when_absent() -> None:
    """Missing X-Request-Id header → response includes generated ID."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/test")

    assert response.status_code == 200
    rid = response.headers.get("x-request-id", "")
    assert rid.startswith("req_"), f"expected req_<hex> prefix, got {rid!r}"
    assert len(rid) == 4 + 26, f"expected 30-char id, got {len(rid)}"


@pytest.mark.asyncio
async def test_propagates_provided_request_id() -> None:
    """Client-supplied X-Request-Id is echoed back unchanged."""
    app = _make_test_app()
    supplied_id = "client-trace-id-12345"
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/test", headers={"X-Request-Id": supplied_id})

    assert response.headers.get("x-request-id") == supplied_id


@pytest.mark.asyncio
async def test_response_always_has_request_id_header() -> None:
    """X-Request-Id is present in response regardless of whether client sent one."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        resp_no_header = await client.get("/v1/test")
        resp_with_header = await client.get("/v1/test", headers={"X-Request-Id": "abc"})

    assert "x-request-id" in resp_no_header.headers
    assert resp_with_header.headers["x-request-id"] == "abc"


@pytest.mark.asyncio
async def test_contextvars_bound_for_route_handler() -> None:
    """structlog contextvars have request_id during request handling."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/test")

    data = response.json()
    assert data["request_id_in_ctx"] != "", "request_id not bound in contextvars"
    # The ID in ctx must match the header echoed back.
    assert data["request_id_in_ctx"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_skip_path_healthz_passes_through() -> None:
    """Health probe paths bypass request-id assignment (no overhead on scrape)."""
    from fastapi import FastAPI
    from src.gateway.middleware.request_id import RequestIDMiddleware
    from starlette.responses import PlainTextResponse, Response

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/healthz")
    async def healthz() -> Response:
        return PlainTextResponse("ok")

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    # Skip-path: no X-Request-Id injected into response headers.
    assert "x-request-id" not in response.headers


@pytest.mark.asyncio
async def test_empty_request_id_header_triggers_generation() -> None:
    """Empty X-Request-Id header is treated as absent — new ID generated."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/test", headers={"X-Request-Id": "   "})

    rid = response.headers.get("x-request-id", "")
    # Whitespace-only input is stripped → treated as empty → new ID generated.
    assert rid.startswith("req_"), f"expected generated id, got {rid!r}"
