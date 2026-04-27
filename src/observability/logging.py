"""
Structured logging configuration using structlog.

Features:
- JSON output to stdout (one line per log event)
- Standard fields: service, env, level, event, ts, request_id
- RedactProcessor: scrubs secrets from log records before they reach output
  (addresses brainstorm §7 secret-leak risk)

Usage:
    from src.observability.logging import configure_logging
    configure_logging(settings)

    import structlog
    logger = structlog.get_logger(__name__)
    logger.info("hello", user_id=42)
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from src.settings import Settings

# Keys matching this pattern have their values replaced with ***REDACTED***.
# Case-insensitive; covers common secret field names used by OpenAI SDKs,
# HTTP headers, and our own config.
_SECRET_KEY_RE = re.compile(
    r"(?i)(authorization|api[-_]?key|codex[-_]?api[-_]?key"
    r"|openai[-_]?api[-_]?key|secret|token|password)"
)

_REDACTED = "***REDACTED***"


def _redact_value(value: Any) -> Any:  # noqa: ANN401
    """Recursively redact secrets nested inside dicts/lists."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(k) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


class RedactProcessor:
    """structlog processor that scrubs sensitive values before rendering.

    Walks the entire event dict and replaces the *values* of keys whose names
    match ``_SECRET_KEY_RE`` with ``***REDACTED***``.  Nested dicts/lists are
    traversed recursively so secrets buried in request/response payloads are
    also scrubbed.
    """

    def __call__(self, logger: WrappedLogger, method: str, event_dict: EventDict) -> EventDict:
        for key in list(event_dict.keys()):
            if _SECRET_KEY_RE.search(key):
                event_dict[key] = _REDACTED
            else:
                event_dict[key] = _redact_value(event_dict[key])
        return event_dict


def configure_logging(settings: Settings) -> None:
    """Install structlog with JSON renderer and redaction processor.

    Must be called once, early in the lifespan, before any log statements.
    Subsequent calls are idempotent (structlog.configure is idempotent).
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Standard library root logger → structlog sink
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        RedactProcessor(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    # Bind service-level context visible on every log record
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        service=settings.otel_service_name,
        env=settings.wrapper_env,
    )
