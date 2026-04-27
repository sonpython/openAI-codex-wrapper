"""
OpenAI-compatible response schemas for POST /v1/chat/completions.

Covers both sync (ChatCompletion) and streaming (ChatCompletionChunk) shapes.
Per researcher-02 §A.2-A.5.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Usage(BaseModel):
    """Token usage — best-effort tiktoken estimate (codex tokens not exposed)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # Extra field to signal best-effort estimate to downstream monitoring.
    # Pydantic extra=allow so serialisation includes it.
    model_config = ConfigDict(extra="allow")


class ResponseMessage(BaseModel):
    """Fully-materialized message in a sync response."""

    role: str
    content: str
    model_config = ConfigDict(extra="allow")


class Choice(BaseModel):
    """Single choice in a sync chat completion response."""

    index: int
    message: ResponseMessage
    finish_reason: str
    logprobs: None = None
    model_config = ConfigDict(extra="allow")


class ChatCompletion(BaseModel):
    """Sync response body — shape per researcher-02 §A.5."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage
    model_config = ConfigDict(extra="allow")


class Delta(BaseModel):
    """Incremental content delta for a streaming chunk."""

    role: str | None = None
    content: str | None = None
    model_config = ConfigDict(extra="allow")


class ChunkChoice(BaseModel):
    """Single choice in a streaming chunk."""

    index: int
    delta: Delta
    finish_reason: str | None = None
    logprobs: None = None
    model_config = ConfigDict(extra="allow")


class ChatCompletionChunk(BaseModel):
    """Streaming chunk body — shape per researcher-02 §A.2-A.3."""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: Usage | None = None
    model_config = ConfigDict(extra="allow")
