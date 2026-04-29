"""
Unit tests for src/chat/tool_calling.py.

Covers:
  parse_tool_response:
    - happy path: valid JSON with known tool name
    - markdown fence stripping (```json ... ```)
    - plain fence stripping (``` ... ```)
    - leading prose before JSON (extraction fallback)
    - invalid tool name → None
    - malformed JSON → None
    - empty tool_calls list → None
    - multi-call: two valid calls
    - missing "arguments" key → None
    - missing "name" key → None
    - top-level not a dict → None
    - missing "tool_calls" key → None

  format_tools_prompt:
    - single tool: contains name + param + description
    - empty tools list → ""
    - tool_choice="none" → ""
    - tool_choice="auto" → injects prompt
    - multiple tools: all names present

  format_assistant_tool_call_for_prompt:
    - assistant with tool_calls emits call summary
    - assistant with no tool_calls falls back to content

  format_tool_result_for_prompt:
    - resolves name via tool_call_id_to_name map
    - falls back to message.name when id not in map
"""

from __future__ import annotations

import json

from src.chat.tool_calling import (
    format_assistant_tool_call_for_prompt,
    format_tool_result_for_prompt,
    format_tools_prompt,
    parse_tool_response,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

VALID_TOOLS: set[str] = {"light_turn_off", "light_turn_on", "get_state"}

SINGLE_CALL_JSON = (
    '{"tool_calls": [{"name": "light_turn_off", "arguments": {"entity_id": "light.living_room"}}]}'
)

MULTI_CALL_JSON = json.dumps(
    {
        "tool_calls": [
            {"name": "light_turn_off", "arguments": {"entity_id": "light.living_room"}},
            {"name": "light_turn_on", "arguments": {"entity_id": "light.kitchen"}},
        ]
    }
)

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
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Get state of an entity",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "attribute": {"type": "string"},
                },
                "required": ["entity_id"],
            },
        },
    },
]


# ── parse_tool_response ────────────────────────────────────────────────────────


def test_parse_happy_path() -> None:
    result = parse_tool_response(SINGLE_CALL_JSON, VALID_TOOLS)
    assert result is not None
    assert len(result) == 1
    assert result[0]["name"] == "light_turn_off"
    assert result[0]["arguments"] == {"entity_id": "light.living_room"}


def test_parse_markdown_json_fence() -> None:
    text = f"```json\n{SINGLE_CALL_JSON}\n```"
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is not None
    assert result[0]["name"] == "light_turn_off"


def test_parse_plain_fence() -> None:
    text = f"```\n{SINGLE_CALL_JSON}\n```"
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is not None
    assert result[0]["name"] == "light_turn_off"


def test_parse_leading_prose_extraction_fallback() -> None:
    """Codex sometimes adds explanatory text before the JSON blob."""
    text = f"Sure, I'll turn off the light.\n{SINGLE_CALL_JSON}"
    result = parse_tool_response(text, VALID_TOOLS)
    # The extraction fallback should pull the JSON object out.
    # This is a best-effort: document trade-off — may be None if prose
    # causes the fence-strip to fail AND extraction returns wrong object.
    # In this case the raw text has the JSON at the end with no fence,
    # so _extract_json_object should find it.
    assert result is not None
    assert result[0]["name"] == "light_turn_off"


def test_parse_invalid_tool_name_returns_none() -> None:
    text = json.dumps({"tool_calls": [{"name": "hallucinated_tool", "arguments": {}}]})
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is None


def test_parse_malformed_json_returns_none() -> None:
    result = parse_tool_response("{not valid json}", VALID_TOOLS)
    assert result is None


def test_parse_empty_tool_calls_returns_none() -> None:
    text = json.dumps({"tool_calls": []})
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is None


def test_parse_multi_call() -> None:
    result = parse_tool_response(MULTI_CALL_JSON, VALID_TOOLS)
    assert result is not None
    assert len(result) == 2
    assert result[0]["name"] == "light_turn_off"
    assert result[1]["name"] == "light_turn_on"


def test_parse_missing_arguments_returns_none() -> None:
    text = json.dumps({"tool_calls": [{"name": "light_turn_off"}]})
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is None


def test_parse_missing_name_returns_none() -> None:
    text = json.dumps({"tool_calls": [{"arguments": {"entity_id": "x"}}]})
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is None


def test_parse_top_level_list_returns_none() -> None:
    text = json.dumps([{"name": "light_turn_off", "arguments": {}}])
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is None


def test_parse_missing_tool_calls_key_returns_none() -> None:
    text = json.dumps({"calls": [{"name": "light_turn_off", "arguments": {}}]})
    result = parse_tool_response(text, VALID_TOOLS)
    assert result is None


def test_parse_empty_text_returns_none() -> None:
    assert parse_tool_response("", VALID_TOOLS) is None
    assert parse_tool_response("   ", VALID_TOOLS) is None


def test_parse_plain_text_returns_none() -> None:
    result = parse_tool_response("The lights are now off.", VALID_TOOLS)
    assert result is None


# ── format_tools_prompt ────────────────────────────────────────────────────────


def test_format_tools_prompt_single_tool_contains_name() -> None:
    prompt = format_tools_prompt(SAMPLE_TOOLS[:1])
    assert "light_turn_off" in prompt
    assert "Turn off a light entity" in prompt


def test_format_tools_prompt_contains_required_param_marker() -> None:
    prompt = format_tools_prompt(SAMPLE_TOOLS[:1])
    # Required params should be marked with '*'
    assert "*entity_id" in prompt


def test_format_tools_prompt_empty_list_returns_empty() -> None:
    assert format_tools_prompt([]) == ""


def test_format_tools_prompt_tool_choice_none_returns_empty() -> None:
    assert format_tools_prompt(SAMPLE_TOOLS, tool_choice="none") == ""


def test_format_tools_prompt_tool_choice_auto_injects() -> None:
    prompt = format_tools_prompt(SAMPLE_TOOLS, tool_choice="auto")
    assert "light_turn_off" in prompt
    assert "get_state" in prompt


def test_format_tools_prompt_all_tools_present() -> None:
    prompt = format_tools_prompt(SAMPLE_TOOLS)
    assert "light_turn_off" in prompt
    assert "get_state" in prompt


def test_format_tools_prompt_contains_instructions() -> None:
    prompt = format_tools_prompt(SAMPLE_TOOLS)
    assert "INSTRUCTIONS" in prompt
    assert "tool_calls" in prompt


def test_format_tools_prompt_no_tool_choice_injects() -> None:
    """tool_choice=None (not provided) should still inject."""
    prompt = format_tools_prompt(SAMPLE_TOOLS, tool_choice=None)
    assert "light_turn_off" in prompt


# ── format_assistant_tool_call_for_prompt ─────────────────────────────────────


def test_format_assistant_tool_call_with_tool_calls() -> None:
    msg = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_abc",
                "type": "function",
                "function": {
                    "name": "light_turn_off",
                    "arguments": '{"entity_id": "light.living_room"}',
                },
            }
        ],
    }
    result = format_assistant_tool_call_for_prompt(msg)
    assert "light_turn_off" in result
    assert "light.living_room" in result


def test_format_assistant_tool_call_no_tool_calls_falls_back_to_content() -> None:
    msg = {"role": "assistant", "content": "Hello there.", "tool_calls": None}
    result = format_assistant_tool_call_for_prompt(msg)
    assert "Hello there." in result


# ── format_tool_result_for_prompt ─────────────────────────────────────────────


def test_format_tool_result_resolves_name_via_id_map() -> None:
    msg = {
        "role": "tool",
        "tool_call_id": "call_abc",
        "content": '{"success": true}',
    }
    id_map = {"call_abc": "light_turn_off"}
    result = format_tool_result_for_prompt(msg, id_map)
    assert "light_turn_off" in result
    assert '{"success": true}' in result


def test_format_tool_result_falls_back_to_message_name() -> None:
    msg = {
        "role": "tool",
        "tool_call_id": "call_xyz",
        "name": "get_state",
        "content": "23 degrees",
    }
    result = format_tool_result_for_prompt(msg, {})  # empty map
    assert "get_state" in result
    assert "23 degrees" in result
