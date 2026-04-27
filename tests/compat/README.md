# SDK Compat Test Suite

End-to-end smoke tests that run the official OpenAI Python and Node SDKs against
the codex-wrapper gateway backed by deterministic mock-codex fixtures.

## Prerequisites

- Docker + Docker Compose v2
- `make` (for convenience targets)

## Run locally

```bash
# Full suite (Python + Node) — spins up stack, runs tests, tears down
make test-compat

# Collection-only (verify test discovery, no Docker needed)
make test-compat-collect

# Manual control
docker compose -f docker-compose.test.yml up -d --build --wait
docker compose -f docker-compose.test.yml exec -T test-runner \
    uv run pytest tests/compat/test-python-sdk.py -v
docker compose -f docker-compose.test.yml exec -T test-runner \
    sh -c "cd tests/compat/test_node_sdk && pnpm test"
docker compose -f docker-compose.test.yml down -v
```

## Against an already-running stack

```bash
COMPAT_EXTERNAL_STACK=1 \
COMPAT_BASE_URL=http://localhost:8001 \
COMPAT_ADMIN_TOKEN=test-admin-token \
uv run pytest tests/compat/test-python-sdk.py -v
```

## Test matrix

| # | Test | SDK | Asserts |
|---|------|-----|---------|
| 1 | models.list | Python + Node | contains codex-cli |
| 2 | chat sync | Python + Node | object, role, finish_reason, usage |
| 3 | chat stream | Python + Node | role first chunk, finish_reason last chunk |
| 4 | chat stream + include_usage | Python + Node | trailing chunk with usage, choices=[] |
| 5 | responses sync | Python + Node | object="response", output text, usage |
| 6 | responses stream | Python + Node | event taxonomy order |
| 7 | invalid api_key | Python + Node | AuthenticationError 401 |
| 8 | rate limit | Python | RateLimitError 429 |
| 9 | malformed body | Python + Node | BadRequestError 400/422 |
| 10 | oversized prompt | Python + Node | BadRequestError 400/413 |
| 11 | REASON_FIRST stream | Python + Node | reasoning before output_text.delta |
| 12 | ERROR_AUTH prompt | Python + Node | APIError |
| 13 | BIG_OUTPUT stream | Python + Node | ≥10k chars reassembled |
| +1 | raw [DONE] bytes | Python (httpx) | `data: [DONE]` literal in SSE bytes |

## Fixture keywords (mock-codex dispatch)

| Keyword in prompt | Fixture | Description |
|---|---|---|
| `ECHO: <text>` | synthesised | Single agent_message with exact text |
| `REASON_FIRST` | reasoning-first.jsonl | Reasoning item then agent_message |
| `MULTI_ITEM` | multi-item.jsonl | 3 agent_message items |
| `ERROR_AUTH` | error-auth.jsonl | Error event AUTH_INVALID, exit 1 |
| `BIG_OUTPUT` | big-output.jsonl | agent_message ≥10k chars |
| `WITH_USAGE` | with-usage.jsonl | turn.completed with all usage fields |
| _(default)_ | happy-path.jsonl | Single "OK" agent_message |

## Real-codex drift cron

`.github/workflows/compat-real-codex.yml` runs weekly (Sunday 03:00 UTC) against
`@openai/codex@latest`. Requires secrets:
- `CODEX_AUTH_JSON_AGE` — age-encrypted `~/.codex/auth.json`
- `AGE_KEY` — age private key for decryption

On failure an issue is auto-created with triage instructions.
