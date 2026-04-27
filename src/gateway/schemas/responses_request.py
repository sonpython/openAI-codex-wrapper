"""
OpenAI Responses API request schema — POST /v1/responses.

Supported fields: model, input, instructions, stream, temperature,
max_output_tokens, metadata, user.

Deferred/unsupported fields (rejected with 400): tools, tool_choice,
previous_response_id, truncation, parallel_tool_calls, text.format, reasoning.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Rejected field names (400 on any presence) ────────────────────────────────

_REJECTED_FIELDS: dict[str, str] = {
    "tools": "tools are not supported in v1; deferred to v1.1",
    "tool_choice": "tool_choice is not supported in v1; deferred to v1.1",
    "previous_response_id": "previous_response_id is not supported in v1",
    "truncation": "truncation is not supported in v1",
    "parallel_tool_calls": "parallel_tool_calls is not supported in v1",
    "reasoning": "reasoning is not supported in v1; deferred to phase-08",
    "text": "text.format is not supported in v1",
}


# ── Input content parts ───────────────────────────────────────────────────────


class InputTextPart(BaseModel):
    """A text input content part."""

    type: Literal["input_text"]
    text: str
    model_config = ConfigDict(extra="allow")


# Only input_text accepted; input_image etc. rejected at InputItem validator.
InputContentPart = InputTextPart


class InputItem(BaseModel):
    """A single input item when ``input`` is provided as a list."""

    role: Literal["system", "user", "assistant"]
    content: str | list[InputContentPart]
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def reject_non_text_parts(cls, data: Any) -> Any:
        """Reject content parts that are not input_text."""
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") not in (
                        "input_text",
                        None,
                    ):
                        raise ValueError(
                            f"content part type '{part.get('type')}' is not supported; "
                            "only 'input_text' parts are accepted"
                        )
        return data


# ── Top-level request model ───────────────────────────────────────────────────


class ResponsesRequest(BaseModel):
    """OpenAI Responses API request model.

    Validates and rejects unsupported parameters before handler runs.
    extra="ignore" for forward-compat with new fields; explicit reject-list
    is applied in model_validator before pydantic strips unknown fields.
    """

    model: str
    input: str | list[InputItem]
    instructions: str | None = Field(default=None, max_length=32_768)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    metadata: dict[str, str] | None = None
    user: str | None = None

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_fields(cls, data: Any) -> Any:
        """Reject deferred/unsupported fields with OpenAI-shaped error messages."""
        if not isinstance(data, dict):
            return data
        found: list[str] = []
        for field, _msg in _REJECTED_FIELDS.items():
            if data.get(field) is not None:
                found.append(field)
        if found:
            param = found[0]
            raise ValueError(f"unsupported_parameter:{param}:{_REJECTED_FIELDS[param]}")
        return data

    @model_validator(mode="after")
    def validate_input_non_empty(self) -> ResponsesRequest:
        """Reject empty string input or empty/blank list input (mirrors OpenAI 400)."""
        inp = self.input
        if isinstance(inp, str):
            if not inp:
                raise ValueError("input cannot be empty")
        elif isinstance(inp, list):
            if not inp:
                raise ValueError("input cannot be empty")

            # Reject lists where every item has blank/empty content
            def _content_empty(item: InputItem) -> bool:
                c = item.content
                if isinstance(c, str):
                    return not c
                return all(not getattr(p, "text", "").strip() for p in c)

            if all(_content_empty(i) for i in inp):
                raise ValueError("input cannot be empty")
        return self

    @model_validator(mode="after")
    def validate_metadata(self) -> ResponsesRequest:
        """Validate metadata key/value constraints (OpenAI rules)."""
        if self.metadata is None:
            return self
        if len(self.metadata) > 16:
            raise ValueError("metadata must not exceed 16 entries")
        import re

        key_pat = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
        for k, v in self.metadata.items():
            if not key_pat.match(k):
                raise ValueError(f"metadata key '{k}' must match ^[a-zA-Z0-9_-]{{1,64}}$")
            if len(v) > 512:
                raise ValueError(f"metadata value for key '{k}' must be ≤ 512 characters")
        return self
