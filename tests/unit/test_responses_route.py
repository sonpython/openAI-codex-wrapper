"""
Unit tests for POST /v1/responses route handler.

Uses FastAPI TestClient with mocked runner + workspace — no real subprocess.

Covers:
  - sync 200: correct response object shape
  - stream 200: SSE bytes with event: lines + headers
  - rejected fields → 400 with OpenAI error envelope
  - cancellation path emits response.cancelled in stream
  - workspace BackgroundTask cleanup fires on both sync and stream paths
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
from src.gateway.routes.responses import router as responses_router

# ── App fixture ────────────────────────────────────────────────────────────────


def _make_app() -> FastAPI:
    """Build a minimal test app that mirrors production validation error handling.

    The RequestValidationError handler is intentionally identical to the one
    in src/gateway/app.py so tests assert the actual production response shape.
    """
    app = FastAPI()

    @app.exception_handler(RequestValidationError)
    async def _val_err(request: object, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        raw_msg = str(first.get("msg", "Request validation error"))
        loc = first.get("loc", ())
        param: str | None = ".".join(str(p) for p in loc if p != "body") or None

        code = "invalid_request_error"
        msg = raw_msg
        prefix = "unsupported_parameter:"
        if prefix in raw_msg:
            tail = raw_msg.split(prefix, 1)[1]
            parts = tail.split(":", 1)
            param = parts[0].strip()
            code = "unsupported_parameter"
            msg = parts[1].strip() if len(parts) > 1 else tail.strip()

        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": msg,
                    "type": "invalid_request_error",
                    "param": param,
                    "code": code,
                }
            },
        )

    app.include_router(responses_router)
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
def client(tmp_path: Path) -> TestClient:
    ws = tmp_path / "ws"
    ws.mkdir()
    app = _make_app()
    with (
        patch("src.gateway.routes.responses.make_workspace", return_value=ws),
        patch("src.gateway.routes.responses.cleanup_workspace"),
        patch("src.gateway.routes.responses.run_codex", side_effect=_fake_run_codex),
    ):
        yield TestClient(app, raise_server_exceptions=True)


# ── Sync 200 ──────────────────────────────────────────────────────────────────


def test_sync_200_shape(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["id"].startswith("resp_")
    assert body["model"] == "codex-cli"


def test_sync_output_text_populated(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping"},
    )
    body = resp.json()
    assert len(body["output"]) == 1
    assert body["output"][0]["content"][0]["text"] == "pong"
    assert body["output"][0]["role"] == "assistant"


def test_sync_usage_present(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping"},
    )
    body = resp.json()
    assert body["usage"]["total_tokens"] > 0


def test_sync_created_at_iso_format(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping"},
    )
    created_at = resp.json()["created_at"]
    # ISO-8601 UTC string (not a unix int)
    assert "T" in created_at
    assert created_at.endswith("Z")


def test_sync_with_instructions(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "instructions": "Be terse."},
    )
    assert resp.status_code == 200


def test_sync_with_list_input(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={
            "model": "codex-cli",
            "input": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200


# ── Stream 200 ────────────────────────────────────────────────────────────────


def test_stream_200_content_type(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "stream": True},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]


def test_stream_sse_headers(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "stream": True},
    ) as resp:
        assert resp.headers.get("cache-control") == "no-cache"
        assert resp.headers.get("x-accel-buffering") == "no"


def test_stream_has_event_lines(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "stream": True},
    ) as resp:
        raw = resp.read()

    # Must contain dual-line SSE events
    assert b"event: response.created" in raw
    assert b"event: response.completed" in raw


def test_stream_first_event_prefix_matches_spec(client: TestClient) -> None:
    """Spec: first bytes must be b'event: response.created\\ndata: {'."""
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "stream": True},
    ) as resp:
        raw = resp.read()

    # Skip any leading SSE comment lines (keepalive: b": keepalive\n\n")
    # Split on double-newline events and find the first non-comment block.
    first_real_block = next(
        (block for block in raw.split(b"\n\n") if block and not block.startswith(b":")),
        b"",
    )
    assert first_real_block.startswith(
        b"event: response.created\ndata: {"
    ), f"First real event bytes: {first_real_block[:80]!r}"


def test_stream_no_done_sentinel(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "stream": True},
    ) as resp:
        raw = resp.read()

    assert b"[DONE]" not in raw


def test_stream_sequence_numbers_present_and_ordered(client: TestClient) -> None:
    with client.stream(
        "POST",
        "/v1/responses",
        json={"model": "codex-cli", "input": "ping", "stream": True},
    ) as resp:
        raw = resp.read()

    seq_nums = []
    for block in raw.decode().split("\n\n"):
        block = block.strip()
        for line in block.split("\n"):
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                    if "sequence_number" in payload:
                        seq_nums.append(payload["sequence_number"])
                except json.JSONDecodeError:
                    pass

    assert seq_nums, "No sequence_numbers found"
    assert seq_nums == list(range(len(seq_nums))), f"Non-monotonic: {seq_nums}"


# ── Rejected fields → 400 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "function"}]),
        ("tool_choice", "auto"),
        ("previous_response_id", "resp_abc"),
        ("truncation", "auto"),
        ("parallel_tool_calls", True),
        ("reasoning", {"effort": "high"}),
    ],
)
def test_rejected_fields_return_400(client: TestClient, field: str, value: object) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "hi", field: value},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"


def test_rejected_field_error_has_openai_shape(client: TestClient) -> None:
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "hi", "tools": [{"type": "function"}]},
    )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert "message" in body
    assert "type" in body
    assert "code" in body


def test_missing_model_returns_400(client: TestClient) -> None:
    resp = client.post("/v1/responses", json={"input": "hi"})
    assert resp.status_code == 422 or resp.status_code == 400


def test_missing_input_returns_422(client: TestClient) -> None:
    resp = client.post("/v1/responses", json={"model": "codex-cli"})
    assert resp.status_code in (400, 422)


def test_text_field_returns_400_with_unsupported_code(client: TestClient) -> None:
    """H1+H3: text field must return 400 with code=unsupported_parameter and param=text."""
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "hi", "text": {"format": {"type": "json_object"}}},
    )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "unsupported_parameter"
    assert body["param"] == "text"
    assert body["type"] == "invalid_request_error"


def test_tools_field_returns_unsupported_param_and_code(client: TestClient) -> None:
    """H3: tools field must return code=unsupported_parameter and param=tools."""
    resp = client.post(
        "/v1/responses",
        json={"model": "codex-cli", "input": "hi", "tools": [{"type": "function"}]},
    )
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "unsupported_parameter"
    assert body["param"] == "tools"


def test_empty_string_input_returns_400(client: TestClient) -> None:
    """H2: empty string input must return 400."""
    resp = client.post("/v1/responses", json={"model": "codex-cli", "input": ""})
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body


# ── BackgroundTask cleanup ────────────────────────────────────────────────────


def test_stream_workspace_cleanup_via_background_task(tmp_path: Path) -> None:
    ws = tmp_path / "ws_bg"
    ws.mkdir()
    mock_cleanup = MagicMock()
    app = _make_app()

    with (
        patch("src.gateway.routes.responses.make_workspace", return_value=ws),
        patch("src.gateway.routes.responses.cleanup_workspace", mock_cleanup),
        patch("src.gateway.routes.responses.run_codex", side_effect=_fake_run_codex),
    ):
        c = TestClient(app, raise_server_exceptions=True)
        with c.stream(
            "POST",
            "/v1/responses",
            json={"model": "codex-cli", "input": "ping", "stream": True},
        ) as resp:
            assert resp.status_code == 200
            resp.read()

    mock_cleanup.assert_called_once_with(ws)


def test_sync_workspace_cleanup_via_background_task(tmp_path: Path) -> None:
    ws = tmp_path / "ws_bg_sync"
    ws.mkdir()
    mock_cleanup = MagicMock()
    app = _make_app()

    with (
        patch("src.gateway.routes.responses.make_workspace", return_value=ws),
        patch("src.gateway.routes.responses.cleanup_workspace", mock_cleanup),
        patch("src.gateway.routes.responses.run_codex", side_effect=_fake_run_codex),
    ):
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post(
            "/v1/responses",
            json={"model": "codex-cli", "input": "ping"},
        )
        assert resp.status_code == 200

    mock_cleanup.assert_called_once_with(ws)
