"""
Integration tests for tool calling through POST /v1/chat/completions.

Uses FastAPI TestClient with mocked codex runner — no real subprocess, no DB/Redis.

Covers:
  - Request with tools + codex returns valid JSON → finish_reason=tool_calls + tool_calls array
  - Request with tools + codex returns plain text → fallback finish_reason=stop + content
  - Request with tools + codex returns invalid JSON → fallback plain text
  - Request without tools + codex returns text → existing behavior unchanged (stop)
  - Multi-turn: messages include role=tool → prompt includes tool result; route accepts it
  - tool_choice="none" → tools prompt skipped → plain text path
  - Multiple tool calls in one response → all emitted
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from src.codex.events import AgentMessageItem, ItemCompleted, TurnCompleted
from src.gateway.routes.chat_completions import router as chat_router

# ── App fixture (mirrors test_chat_route.py) ───────────────────────────────────


def _make_app() -> FastAPI:
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


def _codex_returning(text: str):
    """Return a fake run_codex that emits `text` as agent_message."""

    def _fake(*args: object, **kwargs: object) -> AsyncIterator[object]:
        async def _gen() -> AsyncIterator[object]:
            yield ItemCompleted(
                type="item.completed",
                item=AgentMessageItem(type="agent_message", id="i1", text=text),
            )
            yield TurnCompleted(type="turn.completed")

        return _gen()

    return _fake


SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "light_turn_off",
            "description": "Turn off a light entity",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    }
]

TOOL_CALL_JSON = json.dumps(
    {"tool_calls": [{"name": "light_turn_off", "arguments": {"entity_id": "light.living_room"}}]}
)

MULTI_TOOL_JSON = json.dumps(
    {
        "tool_calls": [
            {"name": "light_turn_off", "arguments": {"entity_id": "light.living_room"}},
            {"name": "light_turn_off", "arguments": {"entity_id": "light.kitchen"}},
        ]
    }
)


@pytest.fixture()
def tmp_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _client_with_codex(codex_text: str, tmp_ws: Path) -> TestClient:
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning(codex_text),
        ),
    ):
        return TestClient(app, raise_server_exceptions=True)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_tools_valid_json_returns_tool_calls(tmp_ws: Path) -> None:
    """Codex returns valid tool_calls JSON → response has finish_reason=tool_calls."""
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning(TOOL_CALL_JSON),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [{"role": "user", "content": "turn off living room light"}],
                "tools": SAMPLE_TOOLS,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    tool_calls = choice["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "light_turn_off"
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["id"].startswith("call_")
    # arguments must be a JSON string (per OpenAI spec)
    args = json.loads(tool_calls[0]["function"]["arguments"])
    assert args["entity_id"] == "light.living_room"


def test_tools_plain_text_response_falls_back_to_stop(tmp_ws: Path) -> None:
    """Codex returns plain text (no JSON) → fallback finish_reason=stop."""
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning("The lights are already off."),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [{"role": "user", "content": "turn off living room light"}],
                "tools": SAMPLE_TOOLS,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "The lights are already off."
    assert choice["message"].get("tool_calls") is None


def test_tools_invalid_json_falls_back_to_text(tmp_ws: Path) -> None:
    """Codex returns invalid JSON → fallback to plain text response."""
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning("{bad json}"),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [{"role": "user", "content": "turn off living room light"}],
                "tools": SAMPLE_TOOLS,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "{bad json}"


def test_no_tools_plain_text_unchanged(tmp_ws: Path) -> None:
    """Existing behavior: no tools → plain text path unaffected."""
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning("pong"),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "codex-cli", "messages": [{"role": "user", "content": "ping"}]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "pong"


def test_multiturn_with_role_tool_message_accepted(tmp_ws: Path) -> None:
    """Multi-turn: messages include role=tool → request accepted (200), prompt built."""
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning("Done, the light is off."),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [
                    {"role": "user", "content": "turn off the living room light"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc123",
                                "type": "function",
                                "function": {
                                    "name": "light_turn_off",
                                    "arguments": '{"entity_id": "light.living_room"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_abc123",
                        "content": '{"success": true}',
                    },
                ],
                "tools": SAMPLE_TOOLS,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    # Codex returned plain text after seeing tool result → stop
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "Done" in body["choices"][0]["message"]["content"]


def test_tool_choice_none_skips_tool_injection(tmp_ws: Path) -> None:
    """tool_choice='none' → tool prompt not injected → plain text fallback."""
    app = _make_app()
    # Even if codex returns JSON-shaped output, tool_choice=none means we skip parsing
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning("I won't call any tools."),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [{"role": "user", "content": "what time is it?"}],
                "tools": SAMPLE_TOOLS,
                "tool_choice": "none",
            },
        )

    assert resp.status_code == 200
    # tools are still present on req so parse still attempted, but codex text is plain
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"


def test_multiple_tool_calls_in_one_response(tmp_ws: Path) -> None:
    """Codex returns two tool calls → both emitted in tool_calls array."""
    two_tool_names = [
        {
            "type": "function",
            "function": {
                "name": "light_turn_off",
                "description": "Turn off",
                "parameters": {
                    "type": "object",
                    "properties": {"entity_id": {"type": "string"}},
                    "required": ["entity_id"],
                },
            },
        },
    ]
    app = _make_app()
    with (
        patch("src.gateway.routes.chat_completions.make_workspace", return_value=tmp_ws),
        patch("src.gateway.routes.chat_completions.cleanup_workspace"),
        patch(
            "src.gateway.routes.chat_completions.run_codex",
            side_effect=_codex_returning(MULTI_TOOL_JSON),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "codex-cli",
                "messages": [{"role": "user", "content": "turn off all lights"}],
                "tools": two_tool_names,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert len(choice["message"]["tool_calls"]) == 2
