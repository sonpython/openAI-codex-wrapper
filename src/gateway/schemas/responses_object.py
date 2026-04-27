"""
OpenAI Responses API response object schemas.

Covers: ResponseObject, OutputItem, OutputContentPart, ResponseUsage,
ResponseError, and related sub-models used in both sync response body
and per-event payloads during streaming.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class OutputTokensDetails(BaseModel):
    """Breakdown of output token usage."""

    reasoning_tokens: int = 0
    model_config = ConfigDict(extra="allow")


class ResponseUsage(BaseModel):
    """Token usage for a response."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    output_tokens_details: OutputTokensDetails = OutputTokensDetails()
    model_config = ConfigDict(extra="allow")


class ResponseError(BaseModel):
    """Error detail when status=failed."""

    code: str
    message: str
    model_config = ConfigDict(extra="allow")


class OutputTextContent(BaseModel):
    """Text content part inside an output message item."""

    type: Literal["output_text"] = "output_text"
    text: str
    annotations: list[object] = []
    model_config = ConfigDict(extra="allow")


OutputContentPart = OutputTextContent


class OutputItem(BaseModel):
    """A single output item (type=message) in the response."""

    id: str
    type: Literal["message"] = "message"
    status: Literal["in_progress", "completed", "failed", "incomplete"] = "completed"
    role: Literal["assistant"] = "assistant"
    content: list[OutputContentPart] = []
    model_config = ConfigDict(extra="allow")


class ResponseObject(BaseModel):
    """Top-level Responses API response object.

    Returned verbatim on sync path; embedded in response.completed event
    on streaming path.

    Note: ``created_at`` is ISO-8601 UTC string per researcher-02 §B.3.1
    (different from chat which uses Unix int).
    """

    id: str
    object: Literal["response"] = "response"
    created_at: str  # ISO-8601 UTC, e.g. "2026-04-27T10:30:00Z"
    status: Literal["in_progress", "completed", "failed", "cancelled", "incomplete"]
    model: str
    output: list[OutputItem] = []
    usage: ResponseUsage | None = None
    metadata: dict[str, str] | None = None
    error: ResponseError | None = None
    incomplete_details: dict[str, str] | None = None
    model_config = ConfigDict(extra="allow")
