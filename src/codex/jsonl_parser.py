"""
JSONL line parser for Codex CLI ``--json`` stdout stream.

MCP contamination guard (researcher-01 issue #15451):
  Lines that do not start with ``{`` are silently skipped at DEBUG level.
  MCP tools can write arbitrary text to stdout even under ``--json`` mode.

Strict-but-tolerant policy:
  - Valid, known event type  → return parsed ``CodexEvent`` model.
  - Valid JSON, unknown type  → log DEBUG, return None (forward-compat).
  - Valid JSON, known type, schema mismatch → log DEBUG, return None.
  - Invalid JSON             → log WARNING, return None.
  - Non-``{`` line           → log DEBUG, return None.
"""

from __future__ import annotations

import json

import structlog
from pydantic import TypeAdapter, ValidationError

from src.codex.events import CodexEvent
from src.observability.metrics import CODEX_EVENT_TOTAL

logger = structlog.get_logger(__name__)

# Single TypeAdapter for the full discriminated union.
# Instantiated once at module load; thread-safe and reusable.
_adapter: TypeAdapter[CodexEvent] = TypeAdapter(CodexEvent)


def parse_line(line: str) -> CodexEvent | None:
    """Parse one raw stdout line from ``codex exec --json``.

    Returns a typed ``CodexEvent`` or ``None`` (never raises).

    Args:
        line: Raw bytes decoded to str (UTF-8, errors replaced).

    Returns:
        Parsed event model, or ``None`` if the line should be skipped.
    """
    s = line.strip()

    # MCP contamination guard: only attempt JSON parse on object-shaped lines.
    if not s.startswith("{"):
        if s:  # suppress empty-line noise
            logger.debug("codex.stdout.non_json", raw=s[:200])
        return None

    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        logger.warning("codex.stdout.bad_json", raw=s[:200])
        return None

    try:
        event = _adapter.validate_python(obj)
        CODEX_EVENT_TOTAL.labels(type=event.type).inc()
        return event
    except ValidationError as exc:
        # Unknown or schema-mismatched event type — forward-compat skip.
        logger.debug(
            "codex.event.unknown_or_invalid",
            event_type=obj.get("type"),
            err=str(exc)[:200],
        )
        return None
