#!/usr/bin/env python3
"""
Drop-in replacement for the real ``codex`` binary used in deterministic CI testing.

Reads the prompt from stdin or from the last positional CLI argument.
Dispatches to a JSONL fixture file based on keyword matching in the prompt.
Emits fixture lines to stdout, honouring optional inter-line delay.

Fixture matching rules (first match wins):
  "ECHO: <text>"  → emit single agent_message with <text>
  "REASON_FIRST"  → reasoning-first.jsonl
  "MULTI_ITEM"    → multi-item.jsonl
  "ERROR_AUTH"    → error-auth.jsonl
  "BIG_OUTPUT"    → big-output.jsonl
  "WITH_USAGE"    → with-usage.jsonl
  default         → happy-path.jsonl

Environment:
  MOCK_CODEX_DELAY_MS  — inter-line sleep in ms (default 0)
  MOCK_CODEX_FIXTURES  — override fixture directory path

Exit code is read from the last comment line "# exit: N" in the fixture;
defaults to 0 if absent.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

# ── Fixture directory resolution ──────────────────────────────────────────────


def _fixture_dir() -> Path:
    env_override = os.environ.get("MOCK_CODEX_FIXTURES")
    if env_override:
        return Path(env_override)
    # Default: sibling jsonl/ directory relative to this script.
    return Path(__file__).parent / "jsonl"


# ── Prompt extraction ─────────────────────────────────────────────────────────


def _read_prompt(argv: list[str]) -> str:
    """Extract prompt from stdin (if available) or last positional arg."""
    # Read stdin if data is piped or redirected (non-interactive).
    if not sys.stdin.isatty():
        try:
            return sys.stdin.read()
        except Exception:  # noqa: BLE001
            pass

    # Fall back to last positional argument that doesn't start with "-".
    positional = [a for a in argv[1:] if not a.startswith("-")]
    if positional:
        return positional[-1]

    return ""


# ── ECHO: <text> handler — synthesise a single-item fixture on the fly ────────


def _echo_fixture(text: str) -> list[str]:
    """Generate JSONL lines for an ECHO prompt without a fixture file."""
    safe_text = text.strip()
    return [
        json.dumps({"type": "thread.started", "thread_id": "th_test"}),
        json.dumps({"type": "turn.started", "turn_id": "turn_1"}),
        json.dumps(
            {"type": "item.started", "item": {"type": "agent_message", "id": "item_1", "text": ""}}
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "id": "item_1", "text": safe_text},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": len(safe_text.split()),
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                },
            }
        ),
    ]


# ── Fixture file dispatcher ───────────────────────────────────────────────────


def _dispatch(prompt: str) -> tuple[list[str], int]:
    """Return (lines_to_emit, exit_code) based on prompt keywords."""
    fixture_dir = _fixture_dir()

    # ECHO: special case — synthesise inline, no file needed.
    echo_match = re.search(r"ECHO:\s*(.+)", prompt, re.IGNORECASE)
    if echo_match:
        return _echo_fixture(echo_match.group(1)), 0

    # Keyword → fixture file mapping.
    keyword_map: list[tuple[str, str]] = [
        ("REASON_FIRST", "reasoning-first.jsonl"),
        ("MULTI_ITEM", "multi-item.jsonl"),
        ("ERROR_AUTH", "error-auth.jsonl"),
        ("BIG_OUTPUT", "big-output.jsonl"),
        ("WITH_USAGE", "with-usage.jsonl"),
    ]

    fixture_name = "happy-path.jsonl"  # default
    for keyword, fname in keyword_map:
        if keyword in prompt.upper():
            fixture_name = fname
            break

    fixture_path = fixture_dir / fixture_name
    if not fixture_path.exists():
        # Graceful fallback: emit a minimal valid sequence.
        sys.stderr.write(f"mock-codex: fixture not found: {fixture_path}\n")
        return _echo_fixture("OK"), 0

    raw_lines = fixture_path.read_text(encoding="utf-8").splitlines()

    # Extract exit code from trailing comment line "# exit: N".
    exit_code = 0
    emit_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if stripped.startswith("# exit:"):
            with contextlib.suppress(ValueError):
                exit_code = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("#") or not stripped:
            # Skip comment-only lines and blank lines.
            continue
        else:
            emit_lines.append(stripped)

    return emit_lines, exit_code


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    delay_ms = int(os.environ.get("MOCK_CODEX_DELAY_MS", "0"))
    delay_s = delay_ms / 1000.0

    prompt = _read_prompt(sys.argv)
    lines, exit_code = _dispatch(prompt)

    for line in lines:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if delay_s > 0:
            time.sleep(delay_s)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
