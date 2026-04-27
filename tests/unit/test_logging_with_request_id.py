"""
Unit tests: structlog contextvars carry request_id within a request scope.

Verifies the integration between RequestIDMiddleware and structlog:
- When RequestIDMiddleware processes a request, structlog.contextvars gets
  request_id bound.
- Log records emitted during the request contain the request_id field.
- After request ends, contextvars are cleaned up (no bleed).

Uses a CapturingProcessor to intercept log events without actual I/O.
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from structlog.types import EventDict, WrappedLogger

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


class CapturingProcessor:
    """structlog processor that appends event_dicts to a list for inspection."""

    def __init__(self) -> None:
        self.records: list[EventDict] = []

    def __call__(self, logger: WrappedLogger, method: str, event_dict: EventDict) -> EventDict:
        # Shallow copy so mutations after this processor don't affect captured state.
        self.records.append(dict(event_dict))
        return event_dict


def _make_app_with_capturing_processor(
    capture: CapturingProcessor,
) -> object:
    """Build FastAPI app with RequestIDMiddleware + a route that logs."""
    import structlog
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from src.gateway.middleware.request_id import RequestIDMiddleware
    from starlette.responses import Response

    # Install a minimal structlog config with our capture processor.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            capture,
            structlog.processors.KeyValueRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    route_logger = structlog.get_logger("test.route")

    @app.get("/v1/log-test")
    async def log_test() -> Response:
        route_logger.info("inside.request")
        ctx = structlog.contextvars.get_contextvars()
        return JSONResponse({"request_id": ctx.get("request_id", "")})

    return app


@pytest.mark.asyncio
async def test_log_line_includes_request_id_from_contextvars() -> None:
    """Log emitted inside a request contains request_id from contextvars."""
    capture = CapturingProcessor()
    app = _make_app_with_capturing_processor(capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/log-test")

    assert response.status_code == 200

    # Find the "inside.request" log record.
    inside_records = [r for r in capture.records if r.get("event") == "inside.request"]
    assert inside_records, "Expected log record 'inside.request' not found"

    record = inside_records[0]
    assert "request_id" in record, f"request_id missing from log record: {record}"
    assert record["request_id"], "request_id must be non-empty"


@pytest.mark.asyncio
async def test_log_request_id_matches_response_header() -> None:
    """The request_id in log contextvars matches the X-Request-Id response header."""
    capture = CapturingProcessor()
    app = _make_app_with_capturing_processor(capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/log-test")

    header_rid = response.headers.get("x-request-id", "")
    assert header_rid, "X-Request-Id header missing from response"

    inside_records = [r for r in capture.records if r.get("event") == "inside.request"]
    assert inside_records, "Log record not found"

    log_rid = inside_records[0].get("request_id", "")
    assert (
        log_rid == header_rid
    ), f"request_id in log ({log_rid!r}) != response header ({header_rid!r})"


@pytest.mark.asyncio
async def test_client_supplied_id_propagated_to_log() -> None:
    """Client-supplied X-Request-Id is bound to contextvars and appears in logs."""
    capture = CapturingProcessor()
    app = _make_app_with_capturing_processor(capture)

    client_rid = "my-trace-id-xyz"
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        await client.get("/v1/log-test", headers={"X-Request-Id": client_rid})

    inside_records = [r for r in capture.records if r.get("event") == "inside.request"]
    assert inside_records, "Log record not found"
    assert inside_records[0].get("request_id") == client_rid


@pytest.mark.asyncio
async def test_request_id_absent_between_requests() -> None:
    """After request ends, contextvars are cleared (no bleed to next request)."""

    capture = CapturingProcessor()
    app = _make_app_with_capturing_processor(capture)

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as client:
        r1 = await client.get("/v1/log-test")
        r2 = await client.get("/v1/log-test")

    rid1 = r1.headers.get("x-request-id")
    rid2 = r2.headers.get("x-request-id")

    # Two different requests must have different generated IDs.
    assert rid1 != rid2, "Each request must get a unique request_id"
