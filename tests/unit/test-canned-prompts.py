"""
Unit tests for tests/fixtures/canned-prompts.json completeness and correctness.

Validates:
- JSON is parseable and schema-correct
- Each entry has required fields
- Every expected_event_type references a known CodexEvent type
- Mock-codex dispatches each canned prompt without parse errors
- Each canned prompt produces at least the expected event types
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CANNED_PROMPTS_PATH = Path(__file__).parent.parent / "fixtures" / "canned-prompts.json"
MOCK_CODEX = Path(__file__).parent.parent / "fixtures" / "mock-codex.py"
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "jsonl"

# Known top-level event types from src/codex/events.py
KNOWN_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "item.started",
    "item.updated",
    "item.completed",
    "turn.completed",
    "turn.failed",
    "error",
}

REQUIRED_FIELDS = {"id", "prompt", "description", "expected_event_types"}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def canned_prompts() -> list[dict]:  # type: ignore[type-arg]
    """Load and return the canned-prompts.json array."""
    return json.loads(CANNED_PROMPTS_PATH.read_text(encoding="utf-8"))


# ── Schema validation ─────────────────────────────────────────────────────────


def test_canned_prompts_file_exists() -> None:
    assert CANNED_PROMPTS_PATH.exists(), f"Missing: {CANNED_PROMPTS_PATH}"


def test_canned_prompts_is_non_empty_list(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    assert isinstance(canned_prompts, list)
    assert len(canned_prompts) >= 5, "Expect at least 5 canned prompts"


def test_all_entries_have_required_fields(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    for entry in canned_prompts:
        missing = REQUIRED_FIELDS - set(entry.keys())
        assert not missing, f"Entry {entry.get('id')!r} missing fields: {missing}"


def test_all_ids_are_unique(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    ids = [e["id"] for e in canned_prompts]
    assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"


def test_expected_event_types_are_known(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    for entry in canned_prompts:
        for etype in entry["expected_event_types"]:
            assert etype in KNOWN_EVENT_TYPES, (
                f"Entry {entry['id']!r} references unknown event type {etype!r}. "
                f"Known: {sorted(KNOWN_EVENT_TYPES)}"
            )


def test_error_entries_have_expected_error_code(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    for entry in canned_prompts:
        if entry.get("expect_error"):
            assert (
                "expected_error_code" in entry
            ), f"Entry {entry['id']!r} has expect_error=true but no expected_error_code"
            assert isinstance(entry["expected_error_code"], str)
            assert entry["expected_error_code"]


def test_all_prompts_are_non_empty_strings(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    for entry in canned_prompts:
        assert isinstance(entry["prompt"], str), f"Entry {entry['id']!r}: prompt must be str"
        assert entry["prompt"].strip(), f"Entry {entry['id']!r}: prompt is empty"


# ── Mock-codex integration (no Docker required) ───────────────────────────────


def _run_mock_codex(prompt: str) -> subprocess.CompletedProcess[str]:
    import os

    env = os.environ.copy()
    env["MOCK_CODEX_FIXTURES"] = str(FIXTURE_DIR)

    return subprocess.run(
        [sys.executable, str(MOCK_CODEX)],
        input=prompt,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )


def _parse_event_types(stdout: str) -> list[str]:
    types = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            try:
                obj = json.loads(stripped)
                types.append(obj.get("type", ""))
            except json.JSONDecodeError:
                pass
    return types


@pytest.mark.parametrize(
    "entry",
    json.loads(CANNED_PROMPTS_PATH.read_text(encoding="utf-8")),
    ids=[e["id"] for e in json.loads(CANNED_PROMPTS_PATH.read_text(encoding="utf-8"))],
)
def test_mock_codex_handles_each_canned_prompt(entry: dict) -> None:  # type: ignore[type-arg]
    """Each canned prompt runs through mock-codex without errors and produces expected events."""
    proc = _run_mock_codex(entry["prompt"])

    expect_error = entry.get("expect_error", False)
    if expect_error:
        assert (
            proc.returncode != 0
        ), f"Entry {entry['id']!r}: expected non-zero exit for error prompt"
    else:
        assert proc.returncode == 0, (
            f"Entry {entry['id']!r}: unexpected exit {proc.returncode}\n"
            f"stderr: {proc.stderr[:500]}"
        )

    emitted_types = set(_parse_event_types(proc.stdout))
    for expected_type in entry["expected_event_types"]:
        assert expected_type in emitted_types, (
            f"Entry {entry['id']!r}: expected event type {expected_type!r} "
            f"not found in output. Got: {sorted(emitted_types)}"
        )


def test_error_prompt_emits_correct_error_code(canned_prompts: list[dict]) -> None:  # type: ignore[type-arg]
    """Error-flagged entries produce the declared error code in output."""
    for entry in canned_prompts:
        if not entry.get("expect_error"):
            continue
        proc = _run_mock_codex(entry["prompt"])
        output_text = proc.stdout
        expected_code = entry["expected_error_code"]
        assert expected_code in output_text, (
            f"Entry {entry['id']!r}: expected error code {expected_code!r} "
            f"not found in output:\n{output_text[:500]}"
        )
