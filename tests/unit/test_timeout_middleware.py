"""
Tests for TimeoutMiddleware.

Covers:
  - Slow handler returns 504 with OpenAI-shaped error
  - /healthz bypassed (no timeout)
  - /readyz bypassed
  - /_internal bypassed
  - SSE /events path bypassed (no timeout)
  - Default 30s timeout for unknown routes (not triggered in unit test — just verifies routing)
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.gateway.middleware.timeout import TimeoutMiddleware, _get_timeout
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# ── Unit tests for _get_timeout helper ───────────────────────────────────────


def test_get_timeout_chat(mock_settings):
    t = _get_timeout("/v1/chat/completions", mock_settings)
    assert t == float(mock_settings.chat_default_timeout_seconds)


def test_get_timeout_responses(mock_settings):
    t = _get_timeout("/v1/responses", mock_settings)
    assert t == float(mock_settings.responses_timeout_seconds)


def test_get_timeout_healthz(mock_settings):
    assert _get_timeout("/healthz", mock_settings) is None


def test_get_timeout_readyz(mock_settings):
    assert _get_timeout("/readyz", mock_settings) is None


def test_get_timeout_internal(mock_settings):
    assert _get_timeout("/_internal/metrics", mock_settings) is None


def test_get_timeout_sse_events(mock_settings):
    # SSE stream — no timeout
    assert _get_timeout("/v1/codex/jobs/abc-123/events", mock_settings) is None


def test_get_timeout_default(mock_settings):
    # Unknown route → 30s default
    assert _get_timeout("/v1/models", mock_settings) == 30.0


# ── Integration test: slow handler → 504 ─────────────────────────────────────


@pytest.fixture()
def slow_app(mock_settings, monkeypatch):
    """FastAPI app with TimeoutMiddleware and a 2-second sleep handler."""
    import src.gateway.middleware.timeout as tm

    monkeypatch.setattr(tm, "get_settings", lambda: mock_settings)
    # Override chat timeout to 0.05s so the test runs fast
    mock_settings.chat_default_timeout_seconds = 0  # type: ignore[attr-defined]

    app = FastAPI()
    app.add_middleware(TimeoutMiddleware)

    @app.get("/v1/chat/completions")
    async def _slow(request: Request) -> PlainTextResponse:
        await asyncio.sleep(10)
        return PlainTextResponse("ok")

    return app


def test_slow_handler_returns_504(slow_app, mock_settings, monkeypatch):
    """Handler that sleeps past timeout → 504 with OpenAI error shape."""
    import src.gateway.middleware.timeout as tm

    monkeypatch.setattr(tm, "get_settings", lambda: mock_settings)
    # Patch _get_timeout to return 0.01s for chat path
    monkeypatch.setattr(tm, "_get_timeout", lambda path, s: 0.01 if "chat" in path else None)

    with TestClient(slow_app, raise_server_exceptions=False) as client:
        resp = client.get("/v1/chat/completions")

    assert resp.status_code == 504
    body = resp.json()
    assert body["error"]["type"] == "timeout"
    assert body["error"]["code"] == "timeout"


def test_healthz_not_timed_out(monkeypatch):
    """Requests to /healthz are never timed out."""
    from src.gateway.middleware.timeout import _get_timeout
    from src.settings import get_settings

    settings = get_settings()
    assert _get_timeout("/healthz", settings) is None


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_settings():
    """Minimal settings stub for timeout tests."""

    class _S:
        chat_default_timeout_seconds = 120
        responses_timeout_seconds = 120

    return _S()
