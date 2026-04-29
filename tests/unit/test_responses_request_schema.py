"""
Unit tests for ResponsesRequest schema validation.

Covers:
  - Accepted fields parse correctly
  - Rejected fields (tools, tool_choice, etc.) raise ValueError
  - list[InputItem] with content parts
  - input_image parts rejected
  - metadata constraints (>16 keys, bad key pattern, long value)
  - instructions max_length enforced by pydantic Field
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from pydantic import ValidationError
from src.gateway.schemas.responses_request import InputItem, ResponsesRequest

# ── Happy path ────────────────────────────────────────────────────────────────


def test_minimal_string_input() -> None:
    req = ResponsesRequest(model="codex-cli", input="hello")
    assert req.model == "codex-cli"
    assert req.input == "hello"
    assert req.stream is False


def test_list_input_accepted() -> None:
    req = ResponsesRequest(
        model="m",
        input=[{"role": "user", "content": "hi"}],
    )
    assert isinstance(req.input, list)
    assert req.input[0].role == "user"
    assert req.input[0].content == "hi"


def test_list_input_with_text_parts() -> None:
    req = ResponsesRequest(
        model="m",
        input=[
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ],
    )
    assert isinstance(req.input, list)
    parts = req.input[0].content
    assert isinstance(parts, list)
    assert parts[0].text == "hello"


def test_instructions_accepted() -> None:
    req = ResponsesRequest(model="m", input="x", instructions="Be helpful")
    assert req.instructions == "Be helpful"


def test_metadata_accepted() -> None:
    req = ResponsesRequest(model="m", input="x", metadata={"k": "v"})
    assert req.metadata == {"k": "v"}


def test_temperature_bounds() -> None:
    req = ResponsesRequest(model="m", input="x", temperature=0.7)
    assert req.temperature == 0.7
    with pytest.raises(ValidationError):
        ResponsesRequest(model="m", input="x", temperature=3.0)


def test_max_output_tokens_positive() -> None:
    req = ResponsesRequest(model="m", input="x", max_output_tokens=100)
    assert req.max_output_tokens == 100
    with pytest.raises(ValidationError):
        ResponsesRequest(model="m", input="x", max_output_tokens=0)


# ── Rejected unsupported fields ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "function"}]),
        ("tool_choice", "auto"),
        ("previous_response_id", "resp_abc"),
        ("truncation", "auto"),
        ("parallel_tool_calls", True),
        ("reasoning", {"effort": "high"}),
    ],
)
def test_rejected_fields_raise_validation_error(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc_info:
        ResponsesRequest(model="m", input="x", **{field: value})
    # The error message embeds the param name
    assert field in str(exc_info.value)


def test_rejected_field_error_contains_unsupported_prefix() -> None:
    """Validator encodes param name for route-layer extraction."""
    with pytest.raises(ValidationError) as exc_info:
        ResponsesRequest(model="m", input="x", tools=[{"type": "function"}])
    assert "unsupported_parameter" in str(exc_info.value)


# ── InputItem content-part rejection ─────────────────────────────────────────


def test_input_image_part_rejected() -> None:
    with pytest.raises(ValidationError):
        InputItem(
            role="user",
            content=[{"type": "input_image", "image_url": {"url": "https://x.com/img.png"}}],
        )


# ── Metadata constraints ──────────────────────────────────────────────────────


def test_metadata_too_many_keys() -> None:
    big = {str(i): "val" for i in range(17)}
    with pytest.raises(ValidationError):
        ResponsesRequest(model="m", input="x", metadata=big)


def test_metadata_bad_key_pattern() -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest(model="m", input="x", metadata={"bad key!": "v"})


def test_metadata_value_too_long() -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest(model="m", input="x", metadata={"k": "x" * 513})


def test_metadata_exactly_16_keys_accepted() -> None:
    meta = {str(i): "v" for i in range(16)}
    req = ResponsesRequest(model="m", input="x", metadata=meta)
    assert len(req.metadata) == 16  # type: ignore[arg-type]


# ── instructions length ───────────────────────────────────────────────────────


def test_instructions_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest(model="m", input="x", instructions="x" * 32_769)


# ── H1: text field (text.format) rejected ────────────────────────────────────


def test_text_field_rejected() -> None:
    """H1: text (e.g. text.format) must be rejected as unsupported in v1."""
    with pytest.raises(ValidationError) as exc_info:
        ResponsesRequest(model="m", input="x", text={"format": {"type": "json_object"}})
    assert "unsupported_parameter" in str(exc_info.value)
    assert "text" in str(exc_info.value)


# ── H2: empty input rejected ──────────────────────────────────────────────────


def test_empty_string_input_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ResponsesRequest(model="m", input="")
    assert "empty" in str(exc_info.value).lower()


def test_empty_list_input_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ResponsesRequest(model="m", input=[])
    assert "empty" in str(exc_info.value).lower()


def test_list_with_empty_content_rejected() -> None:
    """H2: list of items that all have blank content must also be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        ResponsesRequest(
            model="m",
            input=[{"role": "user", "content": ""}],
        )
    assert "empty" in str(exc_info.value).lower()
