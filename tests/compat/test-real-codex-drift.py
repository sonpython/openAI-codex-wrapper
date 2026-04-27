"""
Real-codex drift detection tests.

Runs ONLY when ``CODEX_REAL=1`` is set (requires real codex binary + auth).
Each canned prompt from tests/fixtures/canned-prompts.json is executed against
the real ``codex exec --json`` binary and the output is validated via
src/codex/jsonl_parser.py.

Purpose:
  - Detect upstream schema drift in @openai/codex before it breaks production.
  - Report any NEW event types not in our parser model (forward-compat warning).
  - Run weekly via .github/workflows/compat-real-codex.yml (Sunday 03:00 UTC).

Skip condition: CODEX_REAL env var not set to "1" (all local/CI unit runs skip).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.compat

CODEX_REAL = os.environ.get("CODEX_REAL", "0") == "1"
CANNED_PROMPTS_PATH = Path(__file__).parent.parent / "fixtures" / "canned-prompts.json"

# Known event types from src/codex/events.py (CodexEvent discriminated union).
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

# Required events that every successful run must contain.
REQUIRED_SUCCESS_EVENTS = {"thread.started", "turn.completed"}


def _load_canned_prompts() -> list[dict]:  # type: ignore[type-arg]
    return json.loads(CANNED_PROMPTS_PATH.read_text(encoding="utf-8"))


def _run_real_codex(prompt: str) -> subprocess.CompletedProcess[str]:
    """Invoke real codex binary with --json flag."""
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    return subprocess.run(
        [
            codex_bin,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _parse_events(stdout: str) -> tuple[list[dict], list[str]]:  # type: ignore[type-arg]
    """
    Parse stdout via jsonl_parser.parse_line.

    Returns:
        (parsed_events, unknown_type_warnings)
    """
    # Import here so this module is importable without src on PYTHONPATH in skip mode.
    from src.codex.jsonl_parser import parse_line  # noqa: PLC0415

    parsed = []
    unknown_types: list[str] = []

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        event = parse_line(stripped)
        if event is not None:
            parsed.append(event)
        else:
            # Check if it's valid JSON with an unknown type (forward-compat warning).
            try:
                obj = json.loads(stripped)
                etype = obj.get("type", "")
                if etype and etype not in KNOWN_EVENT_TYPES:
                    unknown_types.append(etype)
            except json.JSONDecodeError:
                pass

    return parsed, unknown_types


# ── Skip all tests if CODEX_REAL not set ─────────────────────────────────────

if not CODEX_REAL:
    pytest.skip(
        "Skipping real-codex drift tests: CODEX_REAL=1 not set. "
        "These run only in the weekly CI cron (.github/workflows/compat-real-codex.yml).",
        allow_module_level=True,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def canned_prompts() -> list[dict]:  # type: ignore[type-arg]
    return _load_canned_prompts()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry",
    _load_canned_prompts(),
    ids=[e["id"] for e in _load_canned_prompts()],
)
def test_real_codex_parses_without_errors(entry: dict) -> None:  # type: ignore[type-arg]
    """Each canned prompt produces parseable JSONL with no parse failures."""
    proc = _run_real_codex(entry["prompt"])

    # Error prompts may exit non-zero; that's expected.
    expect_error = entry.get("expect_error", False)
    if not expect_error:
        assert proc.returncode == 0, (
            f"codex exited {proc.returncode} for prompt {entry['id']!r}\n"
            f"stderr: {proc.stderr[:500]}"
        )

    parsed_events, unknown_types = _parse_events(proc.stdout)

    # Must parse at least one valid event.
    assert len(parsed_events) >= 1, (
        f"No valid events parsed for prompt {entry['id']!r}.\n"
        f"stdout (first 500): {proc.stdout[:500]}"
    )

    # Forward-compat warning: new event types are logged, not failed.
    if unknown_types:
        unique_unknown = sorted(set(unknown_types))
        # Print to test output for triage (visible with pytest -v).
        print(
            f"\n[DRIFT WARNING] Prompt {entry['id']!r} produced unknown event types "
            f"not in our parser model: {unique_unknown}\n"
            f"Action: add to src/codex/events.py if these are stable upstream additions.",
            file=sys.stderr,
        )
        # Not a hard failure — drift warning, not breakage.


@pytest.mark.parametrize(
    "entry",
    [e for e in _load_canned_prompts() if not e.get("expect_error")],
    ids=[e["id"] for e in _load_canned_prompts() if not e.get("expect_error")],
)
def test_real_codex_contains_required_events(entry: dict) -> None:  # type: ignore[type-arg]
    """Every non-error canned prompt must produce thread.started and turn.completed."""
    proc = _run_real_codex(entry["prompt"])
    assert proc.returncode == 0

    parsed_events, _ = _parse_events(proc.stdout)
    emitted_types = {e.type for e in parsed_events}  # type: ignore[union-attr]

    for required in REQUIRED_SUCCESS_EVENTS:
        assert required in emitted_types, (
            f"Prompt {entry['id']!r}: missing required event {required!r}. "
            f"Got: {sorted(emitted_types)}"
        )


def test_real_codex_error_prompt_emits_error_event() -> None:
    """ERROR_AUTH-equivalent: error prompts produce error event (not silent failure)."""
    prompts = _load_canned_prompts()
    error_entries = [e for e in prompts if e.get("expect_error")]
    if not error_entries:
        pytest.skip("No error entries in canned-prompts.json")

    for entry in error_entries:
        proc = _run_real_codex(entry["prompt"])
        parsed_events, _ = _parse_events(proc.stdout)
        error_events = [e for e in parsed_events if e.type == "error"]  # type: ignore[union-attr]
        assert len(error_events) >= 1, (
            f"Prompt {entry['id']!r}: expected error event, got none.\n"
            f"stdout: {proc.stdout[:500]}"
        )


def test_no_unexpected_new_event_types_in_bulk() -> None:
    """
    Run all non-error prompts and collect unknown types across the full suite.

    Fails hard if unknown types appear in > 50% of runs (threshold for schema break
    vs. one-off variation). Otherwise emits a consolidated drift report.
    """
    prompts = [e for e in _load_canned_prompts() if not e.get("expect_error")]
    all_unknown: list[str] = []

    for entry in prompts:
        proc = _run_real_codex(entry["prompt"])
        if proc.returncode != 0:
            continue
        _, unknown = _parse_events(proc.stdout)
        all_unknown.extend(unknown)

    if not all_unknown:
        return  # Clean pass

    unique_unknown = sorted(set(all_unknown))
    frequency = {t: all_unknown.count(t) for t in unique_unknown}

    print(
        f"\n[DRIFT REPORT] Unknown event types across all runs: {frequency}",
        file=sys.stderr,
    )

    # Hard fail if unknown types appear more than half of total runs.
    threshold = len(prompts) // 2
    for etype, count in frequency.items():
        assert count <= threshold, (
            f"Event type {etype!r} appeared {count} times (threshold {threshold}). "
            f"This indicates a breaking schema change in @openai/codex. "
            f"Add {etype!r} to src/codex/events.py to resolve."
        )
