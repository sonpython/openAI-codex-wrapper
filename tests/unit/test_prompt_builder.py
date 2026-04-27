"""
Unit tests for src/chat/prompt_builder.py.

Covers:
  - Single user message
  - Multi-turn dialogue role formatting
  - System + user + assistant ordering
  - Text-only list content (ImageContent already rejected upstream)
  - Oversized prompt raises ValueError
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.chat.prompt_builder import build_prompt
from src.gateway.schemas.chat_request import Message


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


def test_single_user_message() -> None:
    result = build_prompt([_msg("user", "Hello")])
    assert result == "User:\nHello\n\nAssistant:\n"


def test_system_user_pair() -> None:
    result = build_prompt(
        [
            _msg("system", "You are helpful."),
            _msg("user", "What is 2+2?"),
        ]
    )
    assert result == "System:\nYou are helpful.\n\nUser:\nWhat is 2+2?\n\nAssistant:\n"


def test_multi_turn_dialogue() -> None:
    result = build_prompt(
        [
            _msg("system", "Be concise."),
            _msg("user", "Ping"),
            _msg("assistant", "Pong"),
            _msg("user", "Again"),
        ]
    )
    lines = result.split("\n\n")
    assert lines[0] == "System:\nBe concise."
    assert lines[1] == "User:\nPing"
    assert lines[2] == "Assistant:\nPong"
    assert lines[3] == "User:\nAgain"
    assert result.endswith("\n\nAssistant:\n")


def test_role_capitalised() -> None:
    result = build_prompt([_msg("user", "x")])
    assert result.startswith("User:")


def test_text_list_content_joined() -> None:
    """List content with only TextContent parts is concatenated."""
    from src.gateway.schemas.chat_request import TextContent

    msg = Message(
        role="user",
        content=[
            TextContent(type="text", text="Hello "),
            TextContent(type="text", text="world"),
        ],
    )
    result = build_prompt([msg])
    assert "Hello world" in result


def test_oversized_prompt_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompt exceeding CHAT_MAX_PROMPT_CHARS raises ValueError."""
    import src.chat.prompt_builder as pb_module
    from src.settings import Settings

    mock_settings = Settings(
        wrapper_env="test",
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        redis_url="redis://localhost:6379/0",
        chat_max_prompt_chars=10,
    )
    # Patch the get_settings reference inside the prompt_builder module directly.
    monkeypatch.setattr(pb_module, "get_settings", lambda: mock_settings)

    with pytest.raises(ValueError, match="exceeds maximum"):
        build_prompt([_msg("user", "This message is definitely longer than 10 chars")])
