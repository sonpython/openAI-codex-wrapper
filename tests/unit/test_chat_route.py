"""
Unit tests for POST /v1/chat/completions route handler.

Uses FastAPI TestClient with mocked runner — no real codex subprocess,
no real DB/Redis. The app fixture bypasses lifespan via a bare app that
only mounts the chat completions router + auth bypass.

Covers:
  - 200 sync response with correct shape
  - 200 streaming response with SSE byte format + headers
  - Unsupported fields → 400 OpenAI envelope
  - Empty messages → 400
  - Workspace created and cleaned up (patched)
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from src.codex.events import (
    AgentMessageItem,
    ItemCompleted,
    TurnCompleted,
)
from src.gateway.routes.chat_completions import router as chat_router

# ── App fixture ────────────────────────────────────────────────────────────────


def _make_app() -> FastAPI:
    """Bare app with chat router; auth middleware bypassed for unit tests."""
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def _val_err(request: object, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        msg = first.get("msg", "validation error")
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": str(msg),
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_request_error",
                }
            },
        )

    app.include_router(chat_router)
    return app


_FIXTURE_EVENTS = [
    ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id="i1", text="pong"),
    ),
    TurnCompleted(type="turn.completed"),
]


def _fake_run_codex(*args: object, **kwargs: object) -> AsyncIterator[object]:
    async def _gen() -> AsyncIterator[object]:
        for evt in _FIXTURE_EVENTS:
            yield evt

    return _gen()


@pytest.fixture()
def client() -> TestClient:
    app = _make_app()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _patch_workspace(tmp_path: Path) -> object:
    """Patch make_workspace → real temp dir; cleanup_workspace → no-op tracker."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace") as mock_cleanup,
        patch("src.gateway.routes.chat_completions.run_codex", side_effect=_fake_run_codex),
    ):
        yield mock_cleanup


# ── Sync tests ─────────────────────────────────────────────────────────────────


def test_sync_200_valid_shape(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "codex-cli", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0
    assert body["id"].startswith("chatcmpl_")


def test_sync_model_echoed(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-4o-mini"


# ── Stream tests ───────────────────────────────────────────────────────────────


def test_stream_200_sse_format(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "codex-cli",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        raw = resp.read()

    lines = [ln for ln in raw.decode().split("\n\n") if ln.strip()]
    data_lines = [ln for ln in lines if ln.startswith("data: ")]
    assert any("[DONE]" in ln for ln in data_lines)
    json_lines = [ln for ln in data_lines if "[DONE]" not in ln]
    assert len(json_lines) >= 2  # at least role chunk + final chunk


def test_stream_headers_present(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "codex-cli",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"


def test_stream_first_chunk_has_role(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "codex-cli",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    ) as resp:
        raw = resp.read()

    chunks = []
    for part in raw.decode().split("\n\n"):
        part = part.strip()
        if part.startswith("data: ") and "[DONE]" not in part:
            chunks.append(json.loads(part[6:]))

    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"


# ── Rejection tests ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "function", "function": {"name": "f"}}]),
        ("functions", [{"name": "fn"}]),
        ("logprobs", True),
        ("n", 3),
        ("response_format", {"type": "json_object"}),
    ],
)
def test_unsupported_fields_return_400(client: TestClient, field: str, value: object) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "codex-cli",
            "messages": [{"role": "user", "content": "hi"}],
            field: value,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"


def test_empty_messages_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "codex-cli", "messages": []},
    )
    assert resp.status_code == 400


def test_missing_model_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400


# ── C3: BackgroundTask workspace cleanup tests ─────────────────────────────────


def test_stream_workspace_cleanup_via_background_task(client: TestClient) -> None:
    """C3 fix: cleanup_workspace must be called via BackgroundTask after stream ends.

    BackgroundTask runs synchronously in TestClient after the response body is
    exhausted — so mock_cleanup is called by the time the context-manager exits.
    """
    mock_cleanup = MagicMock()
    tmp_ws = Path("/tmp/test_ws_c3")
    tmp_ws.mkdir(exist_ok=True)

    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace", mock_cleanup),
        patch("src.gateway.routes.chat_completions.run_codex", side_effect=_fake_run_codex),
    ):
        app = _make_app()
        c = TestClient(app, raise_server_exceptions=True)
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            resp.read()  # exhaust the body so BackgroundTask fires

    # BackgroundTask must have called cleanup_workspace with the workspace path.
    mock_cleanup.assert_called_once_with(tmp_ws)


def test_sync_workspace_cleanup_on_handler_exception(client: TestClient) -> None:
    """C3: sync path finally-block calls cleanup_workspace even when handler raises."""
    mock_cleanup = MagicMock()
    tmp_ws = Path("/tmp/test_ws_c3_sync")
    tmp_ws.mkdir(exist_ok=True)

    def _failing_run(*args: object, **kwargs: object) -> AsyncIterator[object]:
        async def _gen() -> AsyncIterator[object]:
            raise RuntimeError("runner blew up")
            yield  # make it an async generator

        return _gen()

    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace", mock_cleanup),
        patch("src.gateway.routes.chat_completions.run_codex", side_effect=_failing_run),
    ):
        app = _make_app()
        c = TestClient(app, raise_server_exceptions=False)
        resp = c.post(
            "/v1/chat/completions",
            json={"model": "codex-cli", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 500

    # Sync path finally-block must clean up even after exception.
    mock_cleanup.assert_called_once_with(tmp_ws)
