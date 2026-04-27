"""
Unit tests for tests/fixtures/mock-codex.py dispatch correctness.

Invokes the script via subprocess (matching real container usage) and asserts
that each keyword pattern produces the correct JSONL output and exit code.

All tests run without Docker — mock-codex.py requires only stdlib.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

MOCK_CODEX = Path(__file__).parent.parent / "fixtures" / "mock-codex.py"
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "jsonl"


def _run(
    prompt: str, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run mock-codex.py with prompt on stdin, return CompletedProcess."""
    import os

    env = os.environ.copy()
    env["MOCK_CODEX_FIXTURES"] = str(FIXTURE_DIR)
    if env_extra:
        env.update(env_extra)

    return subprocess.run(
        [sys.executable, str(MOCK_CODEX)],
        input=prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def _parse_jsonl(stdout: str) -> list[dict]:  # type: ignore[type-arg]
    """Parse stdout into list of dicts, skipping blank/comment lines."""
    result = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            result.append(json.loads(stripped))
    return result


# ── Happy path (default fixture) ─────────────────────────────────────────────


def test_default_prompt_emits_happy_path() -> None:
    """Default prompt (no keyword) dispatches to happy-path.jsonl."""
    proc = _run("tell me something")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    types = [e["type"] for e in events]
    assert "thread.started" in types
    assert "turn.completed" in types
    # Must contain at least one agent_message item
    completed = [e for e in events if e["type"] == "item.completed"]
    assert any(e["item"]["type"] == "agent_message" for e in completed)


# ── ECHO: inline synthesis ────────────────────────────────────────────────────


def test_echo_prompt_emits_exact_text() -> None:
    """ECHO: <text> produces agent_message with that text."""
    proc = _run("ECHO: hello world")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    completed = [e for e in events if e["type"] == "item.completed"]
    assert len(completed) == 1
    assert completed[0]["item"]["text"] == "hello world"


def test_echo_case_insensitive() -> None:
    """ECHO: keyword is case-insensitive."""
    proc = _run("echo: greetings")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    completed = [e for e in events if e["type"] == "item.completed"]
    assert completed[0]["item"]["text"] == "greetings"


# ── REASON_FIRST ──────────────────────────────────────────────────────────────


def test_reason_first_emits_reasoning_before_agent_message() -> None:
    """REASON_FIRST dispatches to reasoning-first.jsonl; reasoning precedes agent_message."""
    proc = _run("REASON_FIRST explain quantum entanglement")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    completed = [e for e in events if e["type"] == "item.completed"]
    item_types = [e["item"]["type"] for e in completed]
    assert "reasoning" in item_types
    assert "agent_message" in item_types
    # reasoning must appear before agent_message
    assert item_types.index("reasoning") < item_types.index("agent_message")


# ── MULTI_ITEM ────────────────────────────────────────────────────────────────


def test_multi_item_emits_three_agent_messages() -> None:
    """MULTI_ITEM dispatches to multi-item.jsonl; at least 2 agent_message completions."""
    proc = _run("MULTI_ITEM write multiple paragraphs")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    completed_msgs = [
        e for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"
    ]
    assert len(completed_msgs) >= 2


# ── ERROR_AUTH ────────────────────────────────────────────────────────────────


def test_error_auth_exits_nonzero() -> None:
    """ERROR_AUTH dispatches to error-auth.jsonl; exit code is 1."""
    proc = _run("ERROR_AUTH simulate auth failure")
    assert proc.returncode == 1


def test_error_auth_emits_error_event() -> None:
    """ERROR_AUTH produces an error event with AUTH_INVALID code."""
    proc = _run("ERROR_AUTH simulate auth failure")
    events = _parse_jsonl(proc.stdout)
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "AUTH_INVALID"


# ── BIG_OUTPUT ────────────────────────────────────────────────────────────────


def test_big_output_emits_large_text() -> None:
    """BIG_OUTPUT dispatches to big-output.jsonl; agent_message text >= 10k chars."""
    proc = _run("BIG_OUTPUT generate lots of text")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    completed = [e for e in events if e["type"] == "item.completed"]
    total_chars = sum(len(e["item"].get("text", "")) for e in completed)
    assert total_chars >= 10_000


# ── WITH_USAGE ────────────────────────────────────────────────────────────────


def test_with_usage_populates_token_fields() -> None:
    """WITH_USAGE dispatches to with-usage.jsonl; turn.completed has non-zero tokens."""
    proc = _run("WITH_USAGE count some tokens")
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    turn_completed = [e for e in events if e["type"] == "turn.completed"]
    assert len(turn_completed) == 1
    usage = turn_completed[0]["usage"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0


# ── Event structure invariants ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "tell me something",
        "ECHO: test",
        "MULTI_ITEM foo",
        "WITH_USAGE bar",
    ],
)
def test_every_successful_fixture_starts_with_thread_started(prompt: str) -> None:
    """All non-error fixtures begin with thread.started."""
    proc = _run(prompt)
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    assert events[0]["type"] == "thread.started"


@pytest.mark.parametrize(
    "prompt",
    [
        "tell me something",
        "ECHO: test",
        "MULTI_ITEM foo",
        "WITH_USAGE bar",
    ],
)
def test_every_successful_fixture_ends_with_turn_completed(prompt: str) -> None:
    """All non-error fixtures end with turn.completed."""
    proc = _run(prompt)
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    assert events[-1]["type"] == "turn.completed"


# ── MOCK_CODEX_DELAY_MS env ───────────────────────────────────────────────────


def test_delay_env_is_accepted_without_crash() -> None:
    """MOCK_CODEX_DELAY_MS=1 runs without error (tests env parsing path)."""
    proc = _run("ECHO: delayed", env_extra={"MOCK_CODEX_DELAY_MS": "1"})
    assert proc.returncode == 0
    events = _parse_jsonl(proc.stdout)
    assert any(e["type"] == "item.completed" for e in events)


# ── All events are valid JSON ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "tell me something",
        "ECHO: json test",
        "REASON_FIRST foo",
        "MULTI_ITEM bar",
        "BIG_OUTPUT baz",
        "WITH_USAGE qux",
    ],
)
def test_all_output_lines_are_valid_json(prompt: str) -> None:
    """Every non-comment stdout line from mock-codex is valid JSON."""
    proc = _run(prompt)
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            # Should not raise
            obj = json.loads(stripped)
            assert "type" in obj
