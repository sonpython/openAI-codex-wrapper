"""
Unit tests for ChatCompletionRequest validation.

Covers:
  - All explicitly rejected fields return ValueError with clear message
  - n=1 is accepted, n=2 is rejected
  - Image content parts are rejected (vision not v1)
  - stream_options.include_usage is accepted
  - Empty messages list is rejected
  - Valid minimal request is accepted
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.gateway.schemas.chat_request import (
    ChatCompletionRequest,
    StreamOptions,
)


def _valid() -> dict:
    return {"model": "codex-cli", "messages": [{"role": "user", "content": "hi"}]}


# ── Accepted cases ─────────────────────────────────────────────────────────────


def test_minimal_request_accepted() -> None:
    req = ChatCompletionRequest(**_valid())
    assert req.model == "codex-cli"
    assert req.stream is False


def test_n_equals_1_accepted() -> None:
    req = ChatCompletionRequest(**{**_valid(), "n": 1})
    assert req.n == 1


def test_stream_options_include_usage_accepted() -> None:
    req = ChatCompletionRequest(
        **{**_valid(), "stream": True, "stream_options": {"include_usage": True}}
    )
    assert req.stream_options is not None
    assert req.stream_options.include_usage is True


def test_seed_accepted_and_ignored() -> None:
    req = ChatCompletionRequest(**{**_valid(), "seed": 42})
    assert req.seed == 42


def test_temperature_bounds_accepted() -> None:
    req = ChatCompletionRequest(**{**_valid(), "temperature": 0.7})
    assert req.temperature == pytest.approx(0.7)


def test_multi_role_messages_accepted() -> None:
    req = ChatCompletionRequest(
        model="codex-cli",
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "Bye"},
        ],
    )
    assert len(req.messages) == 4


def test_unknown_fields_silently_ignored() -> None:
    req = ChatCompletionRequest(**{**_valid(), "future_param": "x"})
    assert req.model == "codex-cli"


# ── Rejected fields ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "function", "function": {"name": "f"}}]),
        ("functions", [{"name": "fn"}]),
        ("tool_choice", "auto"),
        ("response_format", {"type": "json_object"}),
        ("logprobs", True),
        ("stop", "\n"),
        ("presence_penalty", 0.5),
        ("frequency_penalty", 0.5),
    ],
)
def test_rejected_fields_raise_validation_error(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(**{**_valid(), field: value})
    errors = exc_info.value.errors()
    assert any("unsupported" in str(e.get("msg", "")).lower() for e in errors)


def test_n_greater_than_1_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(**{**_valid(), "n": 2})
    errors = exc_info.value.errors()
    assert any("unsupported" in str(e.get("msg", "")).lower() for e in errors)


def test_empty_messages_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest(model="codex-cli", messages=[])


def test_image_content_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ChatCompletionRequest(
            model="codex-cli",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "http://example.com/img.png"}}
                    ],
                }
            ],
        )
    errors = exc_info.value.errors()
    assert any("image_url" in str(e.get("msg", "")).lower() for e in errors)


def test_temperature_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatCompletionRequest(**{**_valid(), "temperature": 3.0})


def test_stream_options_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        StreamOptions(include_usage=True, unknown_field="x")
