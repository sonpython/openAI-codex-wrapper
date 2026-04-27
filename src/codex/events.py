"""
Pydantic v2 models for every Codex CLI ``--json`` event type.

Sources:
  - researcher-01-codex-jsonl-schema.md §1 (top-level events)
  - researcher-01-codex-jsonl-schema.md §2 (item payload types)

Design choices:
  - ``extra="allow"`` on every model: tolerate forward-compat fields added in
    future Codex versions without breaking the parser.
  - ``AgentMessageItem`` accepts both ``"agent_message"`` and
    ``"assistant_message"`` via ``Literal`` union (researcher-01 §2 note).
  - ``CodexEvent`` is a discriminated union on the ``type`` field. Unknown
    ``type`` values are not listed here — the parser catches ``ValidationError``
    and returns ``None`` (strict-but-tolerant policy).
  - Item payload union uses ``type`` discriminator; ``ItemStarted``,
    ``ItemUpdated``, ``ItemCompleted`` all embed the same ``ItemPayload`` union.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    """Shared config: allow extra fields for forward-compat."""

    model_config = ConfigDict(extra="allow")


# ── Item payload models ───────────────────────────────────────────────────────


class AgentMessageItem(_Base):
    """Text output from the agent; both literal spellings accepted."""

    type: Literal["agent_message", "assistant_message"]
    id: str
    text: str


class ReasoningItem(_Base):
    type: Literal["reasoning"]
    id: str
    text: str | None = None


class CommandExecutionItem(_Base):
    type: Literal["command_execution"]
    id: str
    command: str
    status: str | None = None


class FileChangeItem(_Base):
    type: Literal["file_change"]
    id: str
    path: str
    status: str | None = None


class FileReadItem(_Base):
    type: Literal["file_read"]
    id: str
    path: str


class ToolUseItem(_Base):
    type: Literal["tool_use"]
    id: str
    name: str
    arguments: dict[str, Any] | None = None


class ToolResultItem(_Base):
    type: Literal["tool_result"]
    id: str
    result: Any | None = None


class WebSearchItem(_Base):
    type: Literal["web_search"]
    id: str
    query: str | None = None


class McpServerStartupItem(_Base):
    type: Literal["mcp_server_startup"]
    id: str


class PlanUpdateItem(_Base):
    type: Literal["plan_update"]
    id: str


# Discriminated union of all item payload types on ``type`` field.
ItemPayload = Annotated[
    AgentMessageItem
    | ReasoningItem
    | CommandExecutionItem
    | FileChangeItem
    | FileReadItem
    | ToolUseItem
    | ToolResultItem
    | WebSearchItem
    | McpServerStartupItem
    | PlanUpdateItem,
    Field(discriminator="type"),
]


# ── Top-level event models ────────────────────────────────────────────────────


class ThreadStarted(_Base):
    type: Literal["thread.started"]
    thread_id: str


class TurnStarted(_Base):
    type: Literal["turn.started"]
    turn_id: str | None = None


class ItemStarted(_Base):
    type: Literal["item.started"]
    item: ItemPayload


class ItemUpdated(_Base):
    type: Literal["item.updated"]
    item: ItemPayload


class ItemCompleted(_Base):
    type: Literal["item.completed"]
    item: ItemPayload


class TokenUsage(_Base):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


class TurnCompleted(_Base):
    type: Literal["turn.completed"]
    usage: TokenUsage | None = None


class TurnFailed(_Base):
    type: Literal["turn.failed"]
    usage: TokenUsage | None = None
    error: dict[str, Any] | None = None


class ErrorPayload(_Base):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorEvent(_Base):
    type: Literal["error"]
    error: ErrorPayload


# Discriminated union of all top-level event types on ``type`` field.
# Unknown ``type`` values cause ValidationError in TypeAdapter → parser returns None.
CodexEvent = Annotated[
    ThreadStarted
    | TurnStarted
    | ItemStarted
    | ItemUpdated
    | ItemCompleted
    | TurnCompleted
    | TurnFailed
    | ErrorEvent,
    Field(discriminator="type"),
]
