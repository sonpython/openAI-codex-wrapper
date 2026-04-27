"""
OpenAI Python SDK compat tests — part 1: happy-path and auth cases.

Tests 1–7 + raw-bytes terminator.
Requires docker-compose.test.yml stack (see tests/compat/README.md).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.compat


# ── 1. models.list ────────────────────────────────────────────────────────────


def test_models_list_contains_codex_cli(sync_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.OpenAI = sync_client  # type: ignore[assignment]
    models = client.models.list()
    ids = [m.id for m in models.data]
    assert "codex-cli" in ids, f"codex-cli not found in models: {ids}"


# ── 2. chat completions sync ──────────────────────────────────────────────────


def test_chat_completions_sync_shape(sync_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.OpenAI = sync_client  # type: ignore[assignment]
    resp = client.chat.completions.create(
        model="codex-cli",
        messages=[{"role": "user", "content": "ECHO: hello sync"}],
        stream=False,
    )
    assert resp.object == "chat.completion"
    assert len(resp.choices) == 1
    choice = resp.choices[0]
    assert choice.message.role == "assistant"
    assert choice.message.content is not None
    assert choice.finish_reason == "stop"
    assert resp.usage is not None
    assert resp.usage.total_tokens > 0


# ── 3. chat completions stream — role first, finish_reason last ───────────────


@pytest.mark.asyncio
async def test_chat_completions_stream_order(async_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.AsyncOpenAI = async_client  # type: ignore[assignment]
    stream = await client.chat.completions.create(
        model="codex-cli",
        messages=[{"role": "user", "content": "ECHO: stream order"}],
        stream=True,
    )
    chunks = [c async for c in stream]
    assert len(chunks) >= 2
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[-1].choices[0].finish_reason == "stop"


# ── 4. chat stream with include_usage ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_stream_include_usage(async_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.AsyncOpenAI = async_client  # type: ignore[assignment]
    stream = await client.chat.completions.create(
        model="codex-cli",
        messages=[{"role": "user", "content": "WITH_USAGE token count test"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = [c async for c in stream]
    usage_chunks = [c for c in chunks if c.usage is not None]
    assert len(usage_chunks) >= 1, "No usage chunk found with include_usage=True"
    assert usage_chunks[-1].usage.total_tokens > 0  # type: ignore[union-attr]


# ── 5. responses.create sync ──────────────────────────────────────────────────


def test_responses_create_sync_shape(sync_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.OpenAI = sync_client  # type: ignore[assignment]
    resp = client.responses.create(
        model="codex-cli",
        input="ECHO: responses sync test",
    )
    assert resp.object == "response"
    assert resp.status == "completed"
    assert len(resp.output) >= 1
    output_item = resp.output[0]
    assert output_item.type == "message"
    content = output_item.content[0]
    assert content.type == "output_text"
    assert content.text is not None
    assert resp.usage is not None
    assert resp.usage.total_tokens > 0


# ── 6. responses.create stream — event taxonomy order ─────────────────────────


@pytest.mark.asyncio
async def test_responses_stream_event_taxonomy(async_client: object) -> None:
    import openai  # noqa: PLC0415

    client: openai.AsyncOpenAI = async_client  # type: ignore[assignment]
    event_types: list[str] = []
    async with client.responses.stream(
        model="codex-cli",
        input="ECHO: responses stream taxonomy",
    ) as stream:
        async for event in stream:
            event_types.append(event.type)

    required = [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.completed",
    ]
    for etype in required:
        assert etype in event_types, f"Missing event {etype!r}. Got: {event_types}"

    def _idx(t: str) -> int:
        return next(i for i, e in enumerate(event_types) if e == t)

    assert _idx("response.created") < _idx("response.output_item.added")
    assert _idx("response.output_item.added") < _idx("response.output_text.delta")
    assert _idx("response.output_text.delta") < _idx("response.output_text.done")
    assert _idx("response.output_text.done") < _idx("response.completed")


# ── 7. invalid api key → AuthenticationError ─────────────────────────────────


def test_invalid_api_key_raises_auth_error(compose_stack: tuple[str, str]) -> None:
    import openai  # noqa: PLC0415

    base_url, _ = compose_stack
    bad_client = openai.OpenAI(
        base_url=f"{base_url}/v1",
        api_key="cwk_invalid_key_for_testing",
        timeout=10,
        max_retries=0,
    )
    with pytest.raises(openai.AuthenticationError) as exc_info:
        bad_client.models.list()
    assert exc_info.value.status_code == 401


# ── Raw bytes: data: [DONE] terminator ────────────────────────────────────────


def test_chat_stream_done_terminator_present(raw_http: object) -> None:
    """Verify the SSE stream ends with the literal ``data: [DONE]`` terminator."""
    import httpx  # noqa: PLC0415

    client: httpx.Client = raw_http  # type: ignore[assignment]
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "codex-cli",
            "messages": [{"role": "user", "content": "ECHO: done terminator test"}],
            "stream": True,
        },
        headers={"Content-Type": "application/json"},
    ) as response:
        response.raise_for_status()
        raw_bytes = response.read()

    assert b"data: [DONE]" in raw_bytes, (
        f"data: [DONE] terminator not found in SSE response. "
        f"Last 200 bytes: {raw_bytes[-200:]!r}"
    )
