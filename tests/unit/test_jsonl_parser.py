"""
Unit tests for src/codex/jsonl_parser.py and src/codex/events.py.

Covers:
- Every top-level event type (8 types)
- Every item payload type (10 types) via item.started
- assistant_message alias for agent_message
- Unknown event type → None (DEBUG log)
- Malformed JSON → None (WARNING log)
- Non-{ line → None (silent / DEBUG)
- Extra fields tolerated (extra="allow")
- Empty line → None (no log noise)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.codex.events import (
    AgentMessageItem,
    CommandExecutionItem,
    ErrorEvent,
    FileChangeItem,
    FileReadItem,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    McpServerStartupItem,
    PlanUpdateItem,
    ReasoningItem,
    ThreadStarted,
    ToolResultItem,
    ToolUseItem,
    TurnCompleted,
    TurnFailed,
    TurnStarted,
    WebSearchItem,
)
from src.codex.jsonl_parser import parse_line

FIXTURES = Path(__file__).parent.parent / "fixtures" / "jsonl"


# ── Helper ────────────────────────────────────────────────────────────────────


def _line(obj: dict) -> str:  # type: ignore[type-arg]
    return json.dumps(obj)


# ── Top-level event types ─────────────────────────────────────────────────────


def test_thread_started() -> None:
    evt = parse_line(_line({"type": "thread.started", "thread_id": "t1"}))
    assert isinstance(evt, ThreadStarted)
    assert evt.thread_id == "t1"


def test_turn_started_with_turn_id() -> None:
    evt = parse_line(_line({"type": "turn.started", "turn_id": "turn_001"}))
    assert isinstance(evt, TurnStarted)
    assert evt.turn_id == "turn_001"


def test_turn_started_without_turn_id() -> None:
    evt = parse_line(_line({"type": "turn.started"}))
    assert isinstance(evt, TurnStarted)
    assert evt.turn_id is None


def test_turn_completed_no_usage() -> None:
    evt = parse_line(_line({"type": "turn.completed"}))
    assert isinstance(evt, TurnCompleted)
    assert evt.usage is None


def test_turn_completed_with_usage() -> None:
    evt = parse_line(
        _line(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        )
    )
    assert isinstance(evt, TurnCompleted)
    assert evt.usage is not None
    assert evt.usage.input_tokens == 10
    assert evt.usage.output_tokens == 20


def test_turn_failed() -> None:
    evt = parse_line(_line({"type": "turn.failed", "error": {"code": "err", "message": "oops"}}))
    assert isinstance(evt, TurnFailed)


def test_error_event() -> None:
    evt = parse_line(_line({"type": "error", "error": {"code": "TIMEOUT", "message": "too slow"}}))
    assert isinstance(evt, ErrorEvent)
    assert evt.error.code == "TIMEOUT"


# ── Item event wrappers ───────────────────────────────────────────────────────


def _item_started(item: dict) -> str:  # type: ignore[type-arg]
    return _line({"type": "item.started", "item": item})


def _item_updated(item: dict) -> str:  # type: ignore[type-arg]
    return _line({"type": "item.updated", "item": item})


def _item_completed(item: dict) -> str:  # type: ignore[type-arg]
    return _line({"type": "item.completed", "item": item})


def test_item_started_agent_message() -> None:
    evt = parse_line(_item_started({"type": "agent_message", "id": "i1", "text": "hi"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, AgentMessageItem)
    assert evt.item.text == "hi"


def test_item_started_assistant_message_alias() -> None:
    """assistant_message is an accepted alias for agent_message."""
    evt = parse_line(_item_started({"type": "assistant_message", "id": "i2", "text": "hey"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, AgentMessageItem)


def test_item_started_reasoning() -> None:
    evt = parse_line(_item_started({"type": "reasoning", "id": "i3", "text": "thinking"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, ReasoningItem)


def test_item_started_command_execution() -> None:
    evt = parse_line(_item_started({"type": "command_execution", "id": "i4", "command": "ls"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, CommandExecutionItem)
    assert evt.item.command == "ls"


def test_item_started_file_change() -> None:
    evt = parse_line(_item_started({"type": "file_change", "id": "i5", "path": "/foo.py"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, FileChangeItem)


def test_item_started_file_read() -> None:
    evt = parse_line(_item_started({"type": "file_read", "id": "i6", "path": "/bar.py"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, FileReadItem)


def test_item_started_tool_use() -> None:
    evt = parse_line(_item_started({"type": "tool_use", "id": "i7", "name": "bash"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, ToolUseItem)


def test_item_started_tool_result() -> None:
    evt = parse_line(_item_started({"type": "tool_result", "id": "i8", "result": "output"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, ToolResultItem)


def test_item_started_web_search() -> None:
    evt = parse_line(_item_started({"type": "web_search", "id": "i9", "query": "python"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, WebSearchItem)


def test_item_started_mcp_server_startup() -> None:
    evt = parse_line(_item_started({"type": "mcp_server_startup", "id": "i10"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, McpServerStartupItem)


def test_item_started_plan_update() -> None:
    evt = parse_line(_item_started({"type": "plan_update", "id": "i11"}))
    assert isinstance(evt, ItemStarted)
    assert isinstance(evt.item, PlanUpdateItem)


def test_item_updated_event() -> None:
    evt = parse_line(_item_updated({"type": "command_execution", "id": "i4", "command": "pwd"}))
    assert isinstance(evt, ItemUpdated)


def test_item_completed_event() -> None:
    evt = parse_line(_item_completed({"type": "agent_message", "id": "i1", "text": "done"}))
    assert isinstance(evt, ItemCompleted)


# ── Tolerant / edge-case behaviour ───────────────────────────────────────────


def test_unknown_event_type_returns_none() -> None:
    result = parse_line(_line({"type": "future_unknown_event", "data": "x"}))
    assert result is None


def test_malformed_json_returns_none() -> None:
    result = parse_line("{not valid json")
    assert result is None


def test_non_brace_line_returns_none() -> None:
    result = parse_line("MCP stdout contamination text")
    assert result is None


def test_empty_line_returns_none() -> None:
    result = parse_line("")
    assert result is None


def test_whitespace_only_line_returns_none() -> None:
    result = parse_line("   \n")
    assert result is None


def test_extra_fields_tolerated() -> None:
    """extra="allow" means new fields from future codex versions don't fail."""
    evt = parse_line(
        _line(
            {
                "type": "thread.started",
                "thread_id": "t1",
                "new_future_field": "value",
            }
        )
    )
    assert isinstance(evt, ThreadStarted)


def test_all_fixture_lines_parse(caplog: pytest.LogCaptureFixture) -> None:
    """all_item_types_stream.jsonl must yield 15 events with no WARNING logs."""
    import logging

    fixture = FIXTURES / "all_item_types_stream.jsonl"
    lines = fixture.read_text().splitlines()
    events = [parse_line(line) for line in lines if line.strip()]
    non_none = [e for e in events if e is not None]
    assert len(non_none) == 15, f"Expected 15 events, got {len(non_none)}"
    # No WARNING-level logs should fire for a clean fixture
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"Unexpected warnings: {warnings}"
