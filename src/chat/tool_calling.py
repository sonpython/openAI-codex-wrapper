"""
Tool calling support: prompt injection + response parsing.

Synthesises OpenAI function-calling on top of Codex CLI via prompt-engineering.

Prompt iteration notes (adjust template here if Codex compliance drops):
  - v1: plain JSON with INSTRUCTIONS block; triple-backtick stripping.
  - If Codex wraps in prose: add "DO NOT add any explanation" to INSTRUCTIONS.
  - If Codex adds trailing text: aggressive JSON extraction (find first '{', matching '}').
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

# ── Prompt helpers ─────────────────────────────────────────────────────────────


def format_tools_prompt(
    tools: list[dict[str, Any]],
    tool_choice: object | None = None,
) -> str:
    """Generate the system-prompt section that instructs Codex on tool use.

    Args:
        tools:       List of tool definitions from the OpenAI request (each has
                     ``type="function"`` and a ``function`` sub-dict with name,
                     description, parameters).
        tool_choice: Mirrors OpenAI's tool_choice field. ``"none"`` → return ""
                     (caller skips injection entirely). All other values → inject.

    Returns:
        A string to be prepended as a system message to the prompt, or "" if
        tool injection should be skipped (tool_choice="none" or empty tools list).

    Example output:
        Available tools (only use when the user's request requires an action):
        - light_turn_off(entity_id: string): Turn off a light entity
        - light_turn_on(entity_id: string): Turn on a light entity

        INSTRUCTIONS:
        - To call tool(s), reply ONLY with this JSON (no other text):
          {"tool_calls": [{"name": "...", "arguments": {...}}, ...]}
        - For multiple simultaneous actions, include multiple objects in the array.
        - If no tool is needed, reply naturally as plain text.
        - NEVER mix JSON with prose or explanation.
        - NEVER invent tool names not listed above.
    """
    if not tools:
        return ""
    if tool_choice == "none":
        return ""

    lines: list[str] = [
        "Available tools (only use when the user's request requires an action):",
    ]
    for tool in tools:
        fn = tool.get("function", {})
        name: str = fn.get("name", "unknown")
        description: str = fn.get("description", "")
        params: dict[str, Any] = fn.get("parameters", {})
        properties: dict[str, Any] = params.get("properties", {})
        required: list[str] = params.get("required", [])

        # Format: name(param: type, *param: type): description
        param_parts: list[str] = []
        for prop_name, prop_schema in properties.items():
            prop_type = prop_schema.get("type", "string")
            marker = "*" if prop_name in required else ""
            param_parts.append(f"{marker}{prop_name}: {prop_type}")
        params_str = ", ".join(param_parts)
        lines.append(f"- {name}({params_str}): {description}")

    lines += [
        "",
        "INSTRUCTIONS:",
        "- To call tool(s), reply ONLY with this exact JSON format (no other text):",
        '  {"tool_calls": [{"name": "TOOL_NAME", "arguments": {KEY: VALUE}}, ...]}',
        "- For multiple simultaneous actions, include multiple objects in the array.",
        "- If no tool is needed, reply naturally as plain text.",
        "- NEVER mix JSON with prose or any explanation.",
        "- NEVER invent tool names not listed above.",
        "- Do NOT wrap JSON in markdown code fences.",
    ]
    return "\n".join(lines)


# ── Response parsing ───────────────────────────────────────────────────────────


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers from text."""
    # Match optional language identifier after opening fence
    pattern = r"^```(?:json)?\s*\n?(.*?)\n?```\s*$"
    match = re.match(pattern, text.strip(), re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def _extract_json_object(text: str) -> str | None:
    """Try to extract the first top-level JSON object from text.

    Used as a fallback when Codex adds prose before or after the JSON.
    Finds the first '{' and attempts to locate the matching '}' by
    tracking nesting depth.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_tool_response(text: str, valid_tools: set[str]) -> list[dict[str, Any]] | None:
    """Try to parse Codex output as a tool_calls JSON object.

    Steps:
      1. Strip markdown fences (```json ... ``` and ``` ... ```)
      2. Strip leading/trailing whitespace
      3. json.loads on cleaned text; fallback: extract first JSON object from text
      4. Validate shape: top-level must be {"tool_calls": [...]}
      5. Each call must have "name" (str) and "arguments" (dict)
      6. Each call's "name" must be in valid_tools
      7. tool_calls list must be non-empty
      8. Return list of valid calls, OR None if any check fails

    On any exception or validation failure → return None (caller falls back to
    plain text — never pretend a tool was called).

    Args:
        text:        Raw text output from Codex agent_message.
        valid_tools: Set of tool names from the original request (name whitelist).

    Returns:
        List of dicts [{"name": str, "arguments": dict}, ...] on success,
        or None on any parse/validation failure.
    """
    if not text or not text.strip():
        return None

    cleaned = _strip_markdown_fences(text).strip()

    # Attempt 1: parse cleaned text directly
    parsed: dict[str, Any] | None = None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(cleaned)

    # Attempt 2: extract first JSON object (handles leading/trailing prose)
    if parsed is None:
        extracted = _extract_json_object(cleaned)
        if extracted:
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                parsed = json.loads(extracted)

    if parsed is None:
        return None

    # Validate top-level shape
    if not isinstance(parsed, dict):
        return None
    if "tool_calls" not in parsed:
        return None
    calls = parsed["tool_calls"]
    if not isinstance(calls, list) or len(calls) == 0:
        return None

    # Validate each call
    result: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            return None
        name = call.get("name")
        arguments = call.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return None
        if name not in valid_tools:
            # Unknown tool name — fall back to text (per brainstorm decision)
            return None
        result.append({"name": name, "arguments": arguments})

    return result if result else None


# ── Multi-turn history formatters ──────────────────────────────────────────────


def format_assistant_tool_call_for_prompt(message: dict[str, Any]) -> str:
    """Format an assistant message that called tools, for multi-turn history.

    Args:
        message: Dict with role="assistant" and tool_calls=[{id, type, function:{name, arguments}}, ...]

    Returns:
        Human-readable string for the Codex prompt, e.g.:
          "Assistant called tools:
           - light_turn_off({"entity_id": "light.living_room"})"
    """
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        content = message.get("content") or ""
        return f"Assistant:\n{content}"

    call_lines: list[str] = []
    for call in tool_calls:
        fn = call.get("function", {}) if isinstance(call, dict) else {}
        name = fn.get("name", "unknown") if isinstance(fn, dict) else "unknown"
        args_str = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
        # arguments is a JSON string per OpenAI spec — display as-is
        call_lines.append(f"  - {name}({args_str})")

    calls_block = "\n".join(call_lines)
    return f"Assistant called tools:\n{calls_block}"


def format_tool_result_for_prompt(
    message: dict[str, Any], tool_call_id_to_name: dict[str, str]
) -> str:
    """Format a role=tool message for multi-turn prompt context.

    Args:
        message:              Dict with role="tool", tool_call_id, content (result).
        tool_call_id_to_name: Map from tool_call_id → tool function name, built
                              from preceding assistant messages' tool_calls.

    Returns:
        Human-readable string for the Codex prompt, e.g.:
          "Tool light_turn_off result: {"success": true}"
    """
    tool_call_id: str = message.get("tool_call_id", "")
    content: str = message.get("content") or ""
    name: str = tool_call_id_to_name.get(tool_call_id, message.get("name", "unknown"))
    return f"Tool {name} result: {content}"
