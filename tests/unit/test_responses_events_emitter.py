"""
Unit tests for ResponseEmitter — golden event-sequence assertions.

Validates:
  - Exact event type ORDER: created → in_progress → output_item.added →
    content_part.added → delta(s) → output_text.done → content_part.done →
    output_item.done → completed
  - sequence_number strictly monotonic, starts at 0, no gaps
  - Each emitted SSE payload encodes to ``event: <type>\\ndata: <json>\\n\\n``
    and first bytes match b"event: response.created\\ndata: {"
  - event.type field in JSON matches the ``event:`` line
  - Text chunker: short text (1 chunk), long text (N chunks), spaces
  - Error mapping: ErrorEvent(TIMEOUT) → code="timeout"
  - cancel() yields response.cancelled + is idempotent
  - finalize() after TurnFailed → response.failed
  - Reasoning items skipped without raising
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from src.codex.events import (
    AgentMessageItem,
    ErrorEvent,
    ErrorPayload,
    ItemCompleted,
    ItemStarted,
    ReasoningItem,
    ThreadStarted,
    TokenUsage,
    TurnCompleted,
    TurnFailed,
)
from src.responses.events_emitter import ResponseEmitter, _chunk_text

# ── Fixtures ──────────────────────────────────────────────────────────────────

_RESPONSE_ID = "resp_aabbccddeeff001122334"
_MODEL = "codex-cli"
_CREATED_AT = "2026-04-27T10:00:00Z"


def _make_emitter() -> ResponseEmitter:
    return ResponseEmitter(
        response_id=_RESPONSE_ID,
        model=_MODEL,
        created_at=_CREATED_AT,
        metadata={"user_id": "u1"},
    )


def _agent_item(text: str, item_id: str = "item_abc") -> AgentMessageItem:
    return AgentMessageItem(type="agent_message", id=item_id, text=text)


def _item_started(text: str = "", item_id: str = "item_abc") -> ItemStarted:
    return ItemStarted(
        type="item.started",
        item=AgentMessageItem(type="agent_message", id=item_id, text=text),
    )


def _item_completed(text: str, item_id: str = "item_abc") -> ItemCompleted:
    return ItemCompleted(
        type="item.completed",
        item=AgentMessageItem(type="agent_message", id=item_id, text=text),
    )


# ── Text chunker ──────────────────────────────────────────────────────────────


def test_chunk_short_text_one_chunk() -> None:
    chunks = list(_chunk_text("hello world", size=50))
    assert chunks == ["hello world"]


def test_chunk_long_text_multiple_chunks() -> None:
    # 60-char text with spaces, size=20
    text = "word1 word2 word3 word4 word5 word6 word7 word8 word9 word10"
    chunks = list(_chunk_text(text, size=20))
    assert len(chunks) > 1
    # Char-window chunker: direct concatenation must reconstruct original exactly.
    assert "".join(chunks) == text


def test_chunk_empty_text() -> None:
    assert list(_chunk_text("", size=50)) == []


def test_chunk_single_word_longer_than_size() -> None:
    # Char-window chunker: long word is sliced into fixed-width pieces.
    text = "superlongword"  # 13 chars
    chunks = list(_chunk_text(text, size=5))
    assert chunks == ["super", "longw", "ord"]
    assert "".join(chunks) == text


# ── Golden sequence ───────────────────────────────────────────────────────────


def _run_full_sequence(text: str = "Hello world") -> list[tuple[str, dict]]:
    """Drive emitter through a complete happy-path sequence."""
    emitter = _make_emitter()
    events: list[tuple[str, dict]] = []

    events.extend(emitter.start())
    events.extend(emitter.on_codex_event(ThreadStarted(type="thread.started", thread_id="t1")))
    events.extend(emitter.on_codex_event(_item_started(item_id="item_abc")))
    events.extend(emitter.on_codex_event(_item_completed(text, item_id="item_abc")))
    events.extend(emitter.on_codex_event(TurnCompleted(type="turn.completed")))
    events.extend(emitter.finalize())
    return events


def test_golden_event_order_types() -> None:
    events = _run_full_sequence("Hello world")
    types = [t for t, _ in events]

    # Mandatory prefix
    assert types[0] == "response.created"
    assert types[1] == "response.in_progress"
    assert types[2] == "response.output_item.added"
    assert types[3] == "response.content_part.added"

    # Somewhere in the middle: at least one delta
    assert "response.output_text.delta" in types

    # Mandatory suffix (before completed)
    assert "response.output_text.done" in types
    assert "response.content_part.done" in types
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"


def test_golden_sequence_numbers_monotonic() -> None:
    events = _run_full_sequence("Hello world")
    seq_nums = [payload["sequence_number"] for _, payload in events]
    assert seq_nums == list(range(len(seq_nums))), f"Non-monotonic: {seq_nums}"


def test_golden_sequence_starts_at_zero() -> None:
    events = _run_full_sequence()
    assert events[0][1]["sequence_number"] == 0


def test_payload_type_matches_event_line() -> None:
    """payload['type'] must equal the event: line identifier."""
    events = _run_full_sequence()
    for evt_type, payload in events:
        assert (
            payload["type"] == evt_type
        ), f"Mismatch: event_line={evt_type!r} but payload.type={payload['type']!r}"


# ── SSE byte encoding ─────────────────────────────────────────────────────────


def _to_sse_bytes(evt_type: str, payload: dict) -> bytes:
    return f"event: {evt_type}\ndata: {json.dumps(payload)}\n\n".encode()


def test_first_event_sse_bytes_prefix() -> None:
    """Spec assertion: first emitted event starts with b'event: response.created\\ndata: {'."""
    emitter = _make_emitter()
    first_type, first_payload = next(iter(emitter.start()))
    raw = _to_sse_bytes(first_type, first_payload)
    assert raw.startswith(
        b"event: response.created\ndata: {"
    ), f"First SSE bytes do not match spec: {raw[:60]!r}"


def test_sse_format_has_both_event_and_data_lines() -> None:
    """Every event must produce event: + data: lines (not data-only like chat)."""
    events = _run_full_sequence()
    for evt_type, payload in events:
        raw = _to_sse_bytes(evt_type, payload).decode()
        lines = raw.split("\n")
        assert lines[0].startswith("event: "), f"Missing event: line in {lines}"
        assert lines[1].startswith("data: "), f"Missing data: line in {lines}"
        assert raw.endswith("\n\n"), "SSE event must end with \\n\\n"


def test_no_done_sentinel() -> None:
    """Responses API must NOT emit [DONE] — stream closes on response.completed."""
    events = _run_full_sequence()
    for _, payload in events:
        raw = json.dumps(payload)
        assert "[DONE]" not in raw


# ── Delta chunking in stream ──────────────────────────────────────────────────


def test_long_text_produces_multiple_deltas() -> None:
    long_text = " ".join([f"word{i}" for i in range(30)])  # ~170 chars
    events = _run_full_sequence(long_text)
    delta_events = [(t, p) for t, p in events if t == "response.output_text.delta"]
    assert len(delta_events) > 1, "Long text should produce multiple deltas (chunk_size=50)"


def test_delta_text_reassembles_to_full_text() -> None:
    full_text = "The quick brown fox jumps over the lazy dog and keeps running"
    events = _run_full_sequence(full_text)
    deltas = [p["delta"] for t, p in events if t == "response.output_text.delta"]
    # Char-window chunker: direct concatenation must equal original string exactly.
    assert "".join(deltas) == full_text


def test_output_text_done_has_full_text() -> None:
    full_text = "Hello world from codex"
    events = _run_full_sequence(full_text)
    done_events = [(t, p) for t, p in events if t == "response.output_text.done"]
    assert len(done_events) == 1
    assert done_events[0][1]["text"] == full_text


# ── Error mapping ─────────────────────────────────────────────────────────────


def test_timeout_error_maps_to_timeout_code() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    error_evt = ErrorEvent(
        type="error", error=ErrorPayload(code="TIMEOUT", message="exceeded 120s")
    )
    evts = list(emitter.on_codex_event(error_evt))
    assert len(evts) == 1
    assert evts[0][0] == "error"
    assert evts[0][1]["code"] == "timeout"


def test_nonzero_exit_maps_to_server_error() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    error_evt = ErrorEvent(
        type="error", error=ErrorPayload(code="EXIT_NONZERO", message="codex exited 1")
    )
    evts = list(emitter.on_codex_event(error_evt))
    assert evts[0][1]["code"] == "server_error"


def test_finalize_after_error_emits_response_failed() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    list(
        emitter.on_codex_event(
            ErrorEvent(type="error", error=ErrorPayload(code="EXIT_NONZERO", message="oops"))
        )
    )
    final = list(emitter.finalize())
    assert len(final) == 1
    assert final[0][0] == "response.failed"
    assert final[0][1]["response"]["status"] == "failed"


def test_finalize_after_turn_failed_emits_response_failed() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    list(emitter.on_codex_event(TurnFailed(type="turn.failed")))
    final = list(emitter.finalize())
    assert final[0][0] == "response.failed"


# ── Cancel path ───────────────────────────────────────────────────────────────


def test_cancel_emits_response_cancelled() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    evts = list(emitter.cancel())
    assert len(evts) == 1
    assert evts[0][0] == "response.cancelled"
    assert evts[0][1]["response"]["status"] == "cancelled"


def test_cancel_is_idempotent() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    first = list(emitter.cancel())
    second = list(emitter.cancel())
    assert len(first) == 1
    assert len(second) == 0


def test_cancel_sequence_number_monotonic() -> None:
    emitter = _make_emitter()
    start_evts = list(emitter.start())
    cancel_evts = list(emitter.cancel())
    # cancel seq must be start_seq + 1
    assert cancel_evts[0][1]["sequence_number"] == start_evts[-1][1]["sequence_number"] + 1


# ── Reasoning items skipped ───────────────────────────────────────────────────


def test_reasoning_item_skipped_without_error() -> None:
    """Reasoning items are deferred to phase-08; must not raise."""
    emitter = _make_emitter()
    list(emitter.start())
    reasoning_evt = ItemCompleted(
        type="item.completed",
        item=ReasoningItem(type="reasoning", id="r1", text="thinking..."),
    )
    evts = list(emitter.on_codex_event(reasoning_evt))
    # No events emitted for reasoning in v1
    assert evts == []


# ── completed response has usage ─────────────────────────────────────────────


def test_completed_response_carries_usage_from_turn_completed() -> None:
    emitter = _make_emitter()
    list(emitter.start())
    list(emitter.on_codex_event(ThreadStarted(type="thread.started", thread_id="t1")))
    list(emitter.on_codex_event(_item_started()))
    list(emitter.on_codex_event(_item_completed("hi")))
    list(
        emitter.on_codex_event(
            TurnCompleted(
                type="turn.completed",
                usage=TokenUsage(input_tokens=10, output_tokens=5, reasoning_tokens=0),
            )
        )
    )
    final = list(emitter.finalize())
    assert final[0][0] == "response.completed"
    usage = final[0][1]["response"]["usage"]
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["total_tokens"] == 15


# ── C1: Multi-message output_index correctness ───────────────────────────────


def _run_two_message_sequence(
    text1: str = "First reply", text2: str = "Second reply"
) -> list[tuple[str, dict]]:
    """Drive emitter through two sequential agent_message items."""
    emitter = _make_emitter()
    events: list[tuple[str, dict]] = []

    events.extend(emitter.start())
    events.extend(emitter.on_codex_event(ThreadStarted(type="thread.started", thread_id="t1")))

    # First item
    events.extend(emitter.on_codex_event(_item_started(item_id="item_001")))
    events.extend(emitter.on_codex_event(_item_completed(text1, item_id="item_001")))

    # Second item
    events.extend(emitter.on_codex_event(_item_started(item_id="item_002")))
    events.extend(emitter.on_codex_event(_item_completed(text2, item_id="item_002")))

    events.extend(emitter.on_codex_event(TurnCompleted(type="turn.completed")))
    events.extend(emitter.finalize())
    return events


def test_two_messages_get_distinct_output_indices() -> None:
    """C1: second agent_message must use output_index=1, not 0."""
    events = _run_two_message_sequence()
    item_added = [(t, p) for t, p in events if t == "response.output_item.added"]
    assert len(item_added) == 2
    assert item_added[0][1]["output_index"] == 0
    assert item_added[1][1]["output_index"] == 1


def test_two_messages_item_done_indices_distinct() -> None:
    events = _run_two_message_sequence()
    item_done = [(t, p) for t, p in events if t == "response.output_item.done"]
    assert len(item_done) == 2
    assert item_done[0][1]["output_index"] == 0
    assert item_done[1][1]["output_index"] == 1


def test_two_messages_delta_indices_distinct() -> None:
    events = _run_two_message_sequence("First", "Second")
    deltas = [(t, p) for t, p in events if t == "response.output_text.delta"]
    first_item_deltas = [p for _, p in deltas if p["item_id"] == "item_001"]
    second_item_deltas = [p for _, p in deltas if p["item_id"] == "item_002"]
    assert all(p["output_index"] == 0 for p in first_item_deltas)
    assert all(p["output_index"] == 1 for p in second_item_deltas)


def test_two_messages_completed_has_both_output_items() -> None:
    events = _run_two_message_sequence("First", "Second")
    final = [(t, p) for t, p in events if t == "response.completed"]
    assert len(final) == 1
    output = final[0][1]["response"]["output"]
    assert len(output) == 2
    texts = [item["content"][0]["text"] for item in output]
    assert "First" in texts
    assert "Second" in texts


def test_sequence_numbers_monotonic_across_two_messages() -> None:
    events = _run_two_message_sequence()
    seq_nums = [p["sequence_number"] for _, p in events]
    assert seq_nums == list(range(len(seq_nums))), f"Non-monotonic: {seq_nums}"


# ── H4: chunker whitespace preservation ──────────────────────────────────────


def test_chunk_text_preserves_newlines_exactly() -> None:
    """H4: ``''.join(chunks) == original`` for text with \\n, \\t, multi-space."""
    from src.responses.events_emitter import _chunk_text

    texts = [
        "line1\nline2\nline3",
        "a\t\tb\t\tc",
        "double  space  here",
        "mixed\n\n  \t content",
    ]
    for text in texts:
        chunks = list(_chunk_text(text, size=5))
        assert "".join(chunks) == text, f"Whitespace not preserved for {text!r}"


def test_chunk_text_exact_byte_reconstruction() -> None:
    """H4: delta concatenation must equal ``output_text.done.text`` byte-for-byte."""
    full_text = "Hello\n\nworld\tand  back"
    events = _run_full_sequence(full_text)
    deltas = [p["delta"] for t, p in events if t == "response.output_text.delta"]
    done_events = [p for t, p in events if t == "response.output_text.done"]
    assert done_events, "output_text.done not emitted"
    assert "".join(deltas) == done_events[0]["text"] == full_text


# ── C1 cancel path: open item flushed before response.cancelled ──────────────


def test_cancel_mid_item_emits_done_events_before_cancelled() -> None:
    """M2/C1: cancel() after output_item.added must emit .done before response.cancelled."""
    emitter = _make_emitter()
    events: list[tuple[str, dict]] = []
    events.extend(emitter.start())
    events.extend(emitter.on_codex_event(ThreadStarted(type="thread.started", thread_id="t1")))
    # Open item but do NOT complete it
    events.extend(emitter.on_codex_event(_item_started(item_id="item_partial")))
    events.extend(emitter.cancel())

    types = [t for t, _ in events]
    # response.cancelled must come after output_item.done
    assert "response.output_item.done" in types
    assert "response.content_part.done" in types
    assert types.index("response.output_item.done") < types.index("response.cancelled")
    # Sequence numbers must still be monotonic
    seq_nums = [p["sequence_number"] for _, p in events]
    assert seq_nums == list(range(len(seq_nums)))
