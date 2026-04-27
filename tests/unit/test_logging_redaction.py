"""
Unit tests for the structlog RedactProcessor.

Verifies that secret-looking keys are scrubbed from log event dicts before
they reach any renderer.  This is the primary defence against secret leaks
in log aggregation pipelines (brainstorm §7).
"""

from __future__ import annotations

import pytest
from src.observability.logging import RedactProcessor

_REDACTED = "***REDACTED***"

processor = RedactProcessor()


def _process(event_dict: dict) -> dict:  # type: ignore[type-arg]
    """Run the processor and return the mutated event dict."""
    return processor(None, "info", dict(event_dict))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "Authorization",
        "AUTHORIZATION",
        "api_key",
        "api-key",
        "apikey",
        "openai_api_key",
        "openai-api-key",
        "codex_api_key",
        "codex-api-key",
        "secret",
        "token",
        "password",
        "PASSWORD",
    ],
)
def test_top_level_secret_keys_are_redacted(key: str) -> None:
    """Top-level keys matching the secret pattern are replaced with REDACTED."""
    result = _process({key: "super-secret-value", "event": "test"})
    assert result[key] == _REDACTED
    assert result["event"] == "test"  # non-secret key untouched


def test_non_secret_keys_are_not_redacted() -> None:
    """Keys that don't match the secret pattern are passed through unchanged."""
    result = _process({"user_id": "42", "event": "login", "status": 200})
    assert result["user_id"] == "42"
    assert result["event"] == "login"
    assert result["status"] == 200


def test_nested_dict_secrets_are_redacted() -> None:
    """Secret keys nested inside a dict value are also scrubbed."""
    result = _process(
        {
            "headers": {"authorization": "Bearer sk-abc123", "content-type": "application/json"},
            "event": "request",
        }
    )
    assert result["headers"]["authorization"] == _REDACTED
    assert result["headers"]["content-type"] == "application/json"


def test_list_of_dicts_secrets_are_redacted() -> None:
    """Secret keys inside a list of dicts are scrubbed."""
    result = _process(
        {
            "items": [{"token": "abc"}, {"name": "safe"}],
            "event": "batch",
        }
    )
    assert result["items"][0]["token"] == _REDACTED
    assert result["items"][1]["name"] == "safe"


def test_deeply_nested_secrets_are_redacted() -> None:
    """Secrets nested 3+ levels deep are still scrubbed."""
    result = _process(
        {
            "request": {
                "body": {
                    "credentials": {"password": "hunter2"},
                }
            },
            "event": "deep",
        }
    )
    assert result["request"]["body"]["credentials"]["password"] == _REDACTED
