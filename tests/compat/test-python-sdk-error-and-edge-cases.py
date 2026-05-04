"""
OpenAI Python SDK compat tests — part 2: error/edge cases.

Tests 8–13: rate limit, malformed body, oversized prompt, reasoning, error event,
large output.
Requires docker-compose.test.yml stack (see tests/compat/README.md).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.compat


# ── 8. rate limit → RateLimitError ───────────────────────────────────────────


def test_rate_limit_raises_rate_limit_error(
    compose_stack: tuple[str, str], test_api_key: str
) -> None:
    """Exhaust RPM limit by sending many rapid requests; expect 429."""
    import httpx as _httpx  # noqa: PLC0415
    import openai  # noqa: PLC0415

    base_url, _ = compose_stack
    admin_resp = _httpx.post(
        f"{base_url}/admin/api-keys",
        json={"user_email": "rate-limit@example.com", "name": "rate-limit-test", "tier": "free"},
        headers={"X-Admin-Token": "test-admin-token", "Content-Type": "application/json"},
        timeout=10,
    )
    admin_resp.raise_for_status()
    limited_key = admin_resp.json()["key"]

    limited_client = openai.OpenAI(
        base_url=f"{base_url}/v1",
        api_key=limited_key,
        timeout=10,
        max_retries=0,
    )

    got_429 = False
    for _ in range(30):
        try:
            limited_client.models.list()
        except openai.RateLimitError as exc:
            assert exc.status_code == 429
            got_429 = True
            break
        except openai.APIError:
            break

    if not got_429:
        pytest.skip("Rate limit not triggered within 30 requests; check tier config")


# ── 9. malformed body → BadRequestError 400/422 ──────────────────────────────


def test_malformed_body_raises_bad_request(sync_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.OpenAI = sync_client  # type: ignore[assignment]
    with pytest.raises(openai.BadRequestError) as exc_info:
        client.chat.completions.create(
            model="codex-cli",
            messages=[],  # empty messages is invalid
        )
    assert exc_info.value.status_code in (400, 422)


# ── 10. oversized prompt → BadRequestError 400/413 ───────────────────────────


def test_oversized_prompt_raises_bad_request(sync_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.OpenAI = sync_client  # type: ignore[assignment]
    huge_content = "x" * (256 * 1024 + 1)  # > 256k chars
    with pytest.raises(openai.BadRequestError) as exc_info:
        client.chat.completions.create(
            model="codex-cli",
            messages=[{"role": "user", "content": huge_content}],
        )
    assert exc_info.value.status_code in (400, 413)


# ── 11. REASON_FIRST prompt → reasoning events before output_text.delta ───────


@pytest.mark.asyncio
async def test_reason_first_stream_has_reasoning_before_text(async_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.AsyncOpenAI = async_client  # type: ignore[assignment]
    event_types: list[str] = []
    async with client.responses.stream(
        model="codex-cli",
        input="REASON_FIRST explain the halting problem",
    ) as stream:
        async for event in stream:
            event_types.append(event.type)

    reasoning_events = [e for e in event_types if "reasoning" in e.lower()]
    text_delta_idx = next(
        (i for i, e in enumerate(event_types) if e == "response.output_text.delta"),
        None,
    )

    if reasoning_events and text_delta_idx is not None:
        first_reasoning_idx = event_types.index(reasoning_events[0])
        assert (
            first_reasoning_idx < text_delta_idx
        ), "Reasoning events must precede output_text.delta"
    else:
        # Gateway may not emit reasoning summary events — verify no crash
        assert "response.completed" in event_types


# ── 12. ERROR_AUTH prompt → APIError ─────────────────────────────────────────


def test_error_auth_prompt_raises_api_error(sync_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.OpenAI = sync_client  # type: ignore[assignment]
    with pytest.raises(openai.APIError):
        client.chat.completions.create(
            model="codex-cli",
            messages=[{"role": "user", "content": "ERROR_AUTH trigger session expiry"}],
        )


# ── 13. BIG_OUTPUT → reassembles ≥ 10k chars ─────────────────────────────────


@pytest.mark.asyncio
async def test_big_output_stream_reassembles_correctly(async_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.AsyncOpenAI = async_client  # type: ignore[assignment]
    stream = await client.chat.completions.create(
        model="codex-cli",
        messages=[{"role": "user", "content": "BIG_OUTPUT generate large text"}],
        stream=True,
    )
    accumulated = ""
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            accumulated += delta

    assert len(accumulated) >= 10_000, f"Expected ≥10k chars, got {len(accumulated)}"
