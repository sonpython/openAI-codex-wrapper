"""
Integration smoke test for the Codex runner.

Skipped unless a real ~/.codex/auth.json is present on the test host.
Run manually or in CI with a valid Codex session:

    pytest tests/integration/test_runner_smoke.py -v

Asserts:
- At least one ItemCompleted(AgentMessageItem) event is yielded
- A TurnCompleted event is yielded
- No ErrorEvent is yielded
"""

from __future__ import annotations

from pathlib import Path

import pytest
from src.codex.events import AgentMessageItem, ErrorEvent, ItemCompleted, TurnCompleted
from src.codex.workspace import cleanup_workspace, make_workspace
from src.settings import get_settings

_AUTH_JSON = Path(get_settings().codex_auth_dir) / "auth.json"

pytestmark = pytest.mark.skipif(
    not _AUTH_JSON.exists(),
    reason="No Codex auth session found — skipping live integration test",
)


@pytest.mark.asyncio
async def test_runner_smoke_pong() -> None:
    """Run a trivial prompt and assert a meaningful response stream."""
    from src.codex.runner import run_codex

    settings = get_settings()
    job_id = "smoke-test-001"

    ws = make_workspace(job_id)
    try:
        events = []
        async for evt in run_codex(
            "Reply with the single word: pong",
            allow_write=False,
            workspace_dir=ws,
            timeout=float(settings.job_timeout_seconds),
        ):
            events.append(evt)

        event_types = [type(e).__name__ for e in events]

        # Must have received at least one completed agent message
        agent_message_completions = [
            e
            for e in events
            if isinstance(e, ItemCompleted) and isinstance(e.item, AgentMessageItem)
        ]
        assert agent_message_completions, f"No ItemCompleted(AgentMessageItem) in: {event_types}"

        # Must have received a TurnCompleted
        turn_completions = [e for e in events if isinstance(e, TurnCompleted)]
        assert turn_completions, f"No TurnCompleted in: {event_types}"

        # Must not have received an ErrorEvent
        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert not errors, f"Unexpected ErrorEvent(s): {[e.error for e in errors]}"

    finally:
        cleanup_workspace(ws)
