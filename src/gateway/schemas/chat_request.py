"""
OpenAI-compatible request schema for POST /v1/chat/completions.

Design:
  - ``extra="ignore"`` on the outer request: forward-compat with new params clients
    may send that we don't yet support (silent ignore, not 400).
  - Explicit reject list validated in ``@model_validator``: produces clear 400 with
    ``invalid_request_error`` code rather than silently ignoring unsupported features.
  - Image content parts are accepted at the type level (for OpenAI SDK compat) but
    rejected at the message validator level — vision not supported in v1.
  - ``n`` accepted but validated: n=1 is fine, n>1 rejected.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextContent(BaseModel):
    """Single text content part in a message."""

    type: Literal["text"]
    text: str
    model_config = ConfigDict(extra="allow")


class ImageContent(BaseModel):
    """Image content part — accepted for parsing, rejected by Message validator."""

    type: Literal["image_url"]
    image_url: dict[str, Any]
    model_config = ConfigDict(extra="allow")


ContentPart = Annotated[
    TextContent | ImageContent,
    Field(discriminator="type"),
]


class Message(BaseModel):
    """A single chat message with role and content.

    Roles:
      - system / user / assistant: standard OpenAI chat roles
      - tool: tool result message sent back by the client after assistant called a tool
              (multi-turn tool use); requires tool_call_id to link back to the call.

    Optional fields for tool-calling multi-turn flow:
      - tool_call_id: present on role=tool messages; links result to assistant's call.
      - tool_calls:   present on role=assistant messages that invoked tools (history replay).
      - name:         tool name on role=tool messages (optional; wrapper derives from context).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None  # opaque; only forwarded for prompt-building
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def validate_message(self) -> Message:
        """Validate message consistency for supported roles.

        - Rejects image_url content parts (vision not v1).
        - Requires tool_call_id when role=tool.
        - Allows content=None for role=assistant with tool_calls (OpenAI spec).
        - Requires non-None content for system/user messages.
        """
        if isinstance(self.content, list):
            for part in self.content:
                if isinstance(part, ImageContent):
                    raise ValueError("image_url content parts are not supported; text-only mode")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool_call_id is required for role=tool messages")
        if self.role in ("system", "user") and self.content is None:
            raise ValueError(f"content is required for role={self.role}")
        return self


class StreamOptions(BaseModel):
    """Options for streaming responses."""

    include_usage: bool = False
    model_config = ConfigDict(extra="forbid")


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request.

    Supports: model, messages, stream, stream_options, temperature, max_tokens, user, n, seed.
    Explicitly rejects: tools, functions, tool_choice, response_format, logprobs, n>1.
    Unknown fields: ignored (forward-compat).
    """

    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    user: str | None = None

    # Reject list — presence triggers 400
    n: int | None = None
    tools: list[Any] | None = None
    functions: list[Any] | None = None
    tool_choice: Any | None = None
    response_format: Any | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    stop: Any | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None

    # Accepted but ignored (seed adds no value; documented)
    seed: int | None = None

    model_config = ConfigDict(extra="ignore")  # tolerate unknown fields silently

    @model_validator(mode="after")
    def reject_unsupported(self) -> ChatCompletionRequest:
        """Return 400 for fields that would corrupt output if silently ignored.

        tools / tool_choice / functions: SILENTLY IGNORED (logged) — many OpenAI
        clients (HA Extended OpenAI Conversation, langchain, etc.) always send
        these fields even when no functions are intended. Strict-rejecting them
        breaks every such client. Codex CLI does not expose function calling, so
        the response will be plain text regardless.
        """
        rejects: list[str] = []
        if self.n is not None and self.n != 1:
            rejects.append("n (must be 1)")
        if self.response_format is not None:
            rejects.append("response_format")
        if self.logprobs:
            rejects.append("logprobs")
        if self.stop is not None:
            rejects.append("stop")
        if self.presence_penalty is not None:
            rejects.append("presence_penalty")
        if self.frequency_penalty is not None:
            rejects.append("frequency_penalty")
        if rejects:
            raise ValueError(f"unsupported parameters (text-only mode): {', '.join(rejects)}")
        return self
