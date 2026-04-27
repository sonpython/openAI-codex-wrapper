# Phase 09: OpenAI SDK Compat Tests

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§9 success metrics — SDK compat is the BAR)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (§1 event types, §2 item types — fixtures derived from this)
- OpenAI taxonomy: research/researcher-02-openai-event-taxonomy.md (Part A + B wire format — assertions derived from this)
- Phase 03: phase-03-chat-completions.md (chat handler under test)
- Phase 04: phase-04-responses-api.md (responses handler under test)
- Phase 08: phase-08-hardening.md (admin endpoint provisions test keys)
- Project rules: ../../.claude/rules/development-rules.md

## Overview
- Priority: critical
- Status: pending
- Effort: S
- Description: End-to-end smoke suite that runs the OFFICIAL OpenAI Python and Node SDKs against the wrapper. This is the bar for "OpenAI compat": if the SDK can read our wire format without modification, we ship; if not, we fix. Includes mock codex fixture so tests are deterministic and fast.

## Key Insights
- "Compat" is a binary property at the SDK level: the SDK either parses our SSE or it raises. This makes the test suite a high-signal gate.
- Mock codex (deterministic JSONL fixtures) is mandatory — tests against real codex are flaky (network, ChatGPT session, model variance) and slow (10s+ per call). Real-codex tests are deferred to staging smoke.
- Test mock codex behavior must match researcher-01's schema EXACTLY (`agent_message`, `reasoning`, `turn.completed.usage`). If schema drifts in production, this suite fails first — early warning.
- Coverage gate ≥ 75% on `src/gateway`, `src/codex`, `src/workers` is the minimum bar — high enough to force tests, low enough to not block on glue code.
- Two SDKs (Python + Node) catch different bugs: Python is async/sync mix; Node is event-emitter pattern. SSE parser bugs that pass one often fail the other.

## Requirements

### Functional
- Python SDK (`openai>=1.50`) test cases all pass against running wrapper.
- Node SDK (`openai>=4.50`) test cases all pass.
- Test stack spins via `docker compose -f docker-compose.test.yml up` and is ready in < 30s.
- Mock codex emits canned JSONL fixtures keyed by prompt content (deterministic).
- CI workflow runs full compat suite on every PR; failure blocks merge.
- Coverage report uploaded as artifact; ≥ 75% on key modules.

### Non-Functional
- Full suite < 5 min wall in CI.
- Mock codex < 50ms latency per response.
- Fixtures human-readable (one event per line, comments allowed via shebang convention).
- All Python files ≤ 200 LOC.

## Architecture

```
docker-compose.test.yml:
  postgres:16  ─┐
  redis:7      ─┼─ same as prod compose (no caddy, no otel-collector)
  gateway      │   CODEX_BIN=/usr/local/bin/mock-codex
  worker       │   CODEX_AUTH_DIR=/tmp/fake-auth (fixture)
  test-runner  ─┘   pytest + node container

mock-codex (drop-in replacement for `codex` binary):
  Python script registered as `codex` in PATH.
  Reads CLI args + stdin prompt.
  Looks up fixture file by hashing/keyword-matching prompt content.
  Emits fixture JSONL to stdout, line by line, with optional inter-line delay.
  Exit 0 on success fixture; exit N on error fixture.

fixture matching rules (in test fixtures only):
  prompt contains "ECHO: <text>"  → emit single agent_message "<text>"
  prompt contains "REASON_FIRST"  → emit reasoning then agent_message
  prompt contains "MULTI_ITEM"    → emit 3 agent_messages
  prompt contains "ERROR_AUTH"    → emit error event (AUTH_INVALID), exit 1
  prompt contains "BIG_OUTPUT"    → emit 10k-char agent_message
  default                         → emit "OK" agent_message

test flow per case:
  1. conftest spins compose stack
  2. POST /admin/api-keys (test admin token) → get plaintext test key
  3. instantiate OpenAI client(base_url=http://gateway:8000/v1, api_key=test_key)
  4. exercise SDK call
  5. assert response shape matches OpenAI canonical wire format
  6. teardown: compose down -v
```

## Test Matrix

### Python SDK (`tests/compat/test_python_sdk.py`)

| # | Test | Asserts |
|---|---|---|
| 1 | `client.models.list()` | result has `data[]`, contains `Model(id="codex-cli", ...)` |
| 2 | `client.chat.completions.create(stream=False)` | `ChatCompletion` with `choices[0].message.content == "OK"`, `usage.total_tokens > 0` |
| 3 | `client.chat.completions.create(stream=True)` | Iterate chunks; first chunk has `delta.role=="assistant"`; final chunk has `finish_reason=="stop"`; raw stream ends with `data: [DONE]` |
| 4 | stream + `stream_options={"include_usage":True}` | After final content chunk, additional chunk with `choices==[]` and `usage` populated |
| 5 | `client.responses.create(stream=False)` | `Response` with `output[0].content[0].text == "OK"`, `usage.total_tokens > 0` |
| 6 | `client.responses.create(stream=True)` | Events in order: `response.created` → `response.output_item.added` → `response.content_part.added` → `response.output_text.delta+` → `response.output_text.done` → `response.completed`; each event has `sequence_number` monotonic |
| 7 | invalid api key | `openai.AuthenticationError` raised with `status_code==401` |
| 8 | rate-limit triggered | `openai.RateLimitError` raised with `status_code==429` AND `Retry-After` header present |
| 9 | malformed body | `openai.BadRequestError` raised with `status_code==400` |
| 10 | prompt > 256k | `openai.BadRequestError` raised with `status_code==413` |
| 11 | "REASON_FIRST" prompt via responses stream | `response.reasoning_summary_text.delta` events present BEFORE `response.output_text.delta` |
| 12 | "ERROR_AUTH" prompt | `openai.APIError` raised; SSE error event well-formed |
| 13 | "BIG_OUTPUT" prompt stream | All deltas reassemble to ≥ 10k chars without truncation |

### Node SDK (`tests/compat/test_node_sdk/index.test.ts`)

Vitest. Same matrix as Python (rows 1–13), translated to Node SDK API:
- `client.models.list()` → assert iterator yields codex-cli.
- `client.chat.completions.create({stream:true})` → for-await iteration; `chunk.choices[0].delta.content` accumulates correctly.
- `client.responses.stream(...)` → event handlers `.on('response.output_text.delta', ...)` fire in order.

## Related Code Files

### To create
- `tests/compat/__init__.py`
- `tests/compat/conftest.py` (≤ 180 LOC) — compose-stack fixture, admin-key provisioning, OpenAI client factory.
- `tests/compat/test_python_sdk.py` (≤ 200 LOC) — 13 pytest async cases above.
- `tests/compat/test_node_sdk/package.json` — declares `openai@^4.50.0`, `vitest@^1.6.0`, `tsx`.
- `tests/compat/test_node_sdk/index.test.ts` (≤ 200 LOC) — Vitest equivalent.
- `tests/compat/test_node_sdk/vitest.config.ts`
- `tests/compat/test_node_sdk/tsconfig.json`
- `tests/fixtures/__init__.py`
- `tests/fixtures/mock_codex.py` (≤ 180 LOC) — drop-in `codex` script.
- `tests/fixtures/jsonl/happy-path.jsonl` — single agent_message + turn.completed.
- `tests/fixtures/jsonl/multi-item.jsonl` — 3 agent_messages.
- `tests/fixtures/jsonl/reasoning-first.jsonl` — reasoning + agent_message.
- `tests/fixtures/jsonl/error-auth.jsonl` — error event AUTH_INVALID.
- `tests/fixtures/jsonl/big-output.jsonl` — agent_message with 10k chars.
- `tests/fixtures/jsonl/with-usage.jsonl` — turn.completed.usage populated.
- `docker-compose.test.yml` — mirrors prod compose minus caddy/otel; mounts mock-codex.
- `Dockerfile.test-runner` (≤ 40 LOC) — python+node base for the test-runner container.
- `.github/workflows/compat.yml` — CI workflow.
- `tests/compat/README.md` — how to run locally.

### To modify
- `pyproject.toml` — add `openai>=1.50,<2` and `pytest-cov` to dev deps.
- `Makefile` — add `test-compat` target (`docker compose -f docker-compose.test.yml run --rm test-runner pytest tests/compat`).

### To delete
(none)

## Implementation Steps

1. **Mock codex script** — `tests/fixtures/mock_codex.py`:
   - Shebang `#!/usr/bin/env python3`. Make executable. Mount into container as `/usr/local/bin/codex`.
   - Parse CLI args minimally: detect `--json` flag (always set by wrapper). Read prompt from stdin OR last positional arg.
   - Apply matching rules from §Architecture to choose fixture file.
   - Stream fixture line-by-line to stdout. Apply optional `MOCK_CODEX_DELAY_MS` env (default 0) between lines.
   - Read exit code from fixture trailing comment line `# exit: N` (default 0).
2. **Fixture files** — Each is newline-delimited JSON matching researcher-01 schema. Examples:

   `happy-path.jsonl`:
   ```jsonl
   {"type":"thread.started","thread_id":"th_test"}
   {"type":"turn.started","turn_id":"turn_1"}
   {"type":"item.started","item":{"id":"item_1","type":"agent_message","status":"in_progress"}}
   {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}
   {"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":1,"cached_input_tokens":0,"reasoning_tokens":0}}
   ```

   `error-auth.jsonl`:
   ```jsonl
   {"type":"thread.started","thread_id":"th_test"}
   {"type":"error","error":{"code":"AUTH_INVALID","message":"session expired"}}
   # exit: 1
   ```

3. **conftest.py — compose fixture** — Session-scoped pytest fixture:
   - `subprocess.run(["docker","compose","-f","docker-compose.test.yml","up","-d","--wait"])`.
   - Poll `/healthz` until 200 (timeout 60s).
   - Yield base_url + admin_token.
   - Teardown: `docker compose down -v`.
4. **Admin key provisioner fixture** — Function-scoped: POST `/admin/api-keys` with admin token, return plaintext key. Auto-revoke at teardown.
5. **OpenAI client factory** — Function-scoped: `openai.OpenAI(base_url=base_url+'/v1', api_key=test_key, timeout=30)`. Async variant `openai.AsyncOpenAI` for stream tests.
6. **Test cases (Python)** — Implement rows 1–13 from matrix. Use `pytest.mark.asyncio` for async cases. Pattern:
   ```python
   async def test_chat_stream_done_terminator(async_client):
       stream = await async_client.chat.completions.create(
           model="codex-cli",
           messages=[{"role":"user","content":"ECHO: hello"}],
           stream=True,
       )
       chunks = [c async for c in stream]
       assert chunks[0].choices[0].delta.role == "assistant"
       assert chunks[-1].choices[0].finish_reason == "stop"
       # SDK strips [DONE]; raw assertion done in raw-bytes test below
   ```
   Add ONE raw-bytes test using `httpx` direct to assert `data: [DONE]\n\n` literal terminator.
7. **Test cases (Node)** — Mirror Python tests in TypeScript with Vitest. Run via `pnpm test` in test-runner container. Example:
   ```typescript
   it("chat stream emits role first, finish_reason last", async () => {
     const stream = await client.chat.completions.create({
       model: "codex-cli",
       messages: [{role:"user", content:"ECHO: hello"}],
       stream: true,
     });
     const chunks = [];
     for await (const c of stream) chunks.push(c);
     expect(chunks[0].choices[0].delta.role).toBe("assistant");
     expect(chunks.at(-1)!.choices[0].finish_reason).toBe("stop");
   });
   ```
8. **docker-compose.test.yml** — services: `postgres`, `redis`, `gateway`, `worker`, `test-runner`. Differences from prod:
   - `gateway`/`worker` env: `CODEX_BIN=/usr/local/bin/codex` (mock), `CODEX_AUTH_DIR=/tmp/fake-auth` (created with empty `auth.json` so readiness passes), `WRAPPER_ENV=test`, `ADMIN_TOKEN=test-admin-token`.
   - `test-runner` mounts source + tests + node modules; runs `tail -f /dev/null` then explicit pytest invocation.
9. **Dockerfile.test-runner** — `python:3.12-slim` + nodejs 20 + uv + pnpm. WORKDIR /app. CMD bash placeholder.
10. **CI workflow** — `.github/workflows/compat.yml`:
    ```yaml
    name: compat
    on: [pull_request, push]
    jobs:
      compat:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: docker compose -f docker-compose.test.yml up -d --wait
          - run: docker compose -f docker-compose.test.yml exec -T test-runner uv run pytest tests/compat -q --cov=src --cov-report=xml
          - run: docker compose -f docker-compose.test.yml exec -T test-runner pnpm --dir tests/compat/test_node_sdk test
          - uses: actions/upload-artifact@v4
            with: { name: coverage, path: coverage.xml }
          - run: docker compose -f docker-compose.test.yml down -v
    ```
11. **Coverage gate** — pytest-cov config in `pyproject.toml`: fail under 75% on `src/gateway`, `src/codex`, `src/workers`. Codecov upload optional.
12. **Local dev** — `make test-compat` runs the full suite locally. README documents prereqs.

## Todo List
- [ ] mock_codex.py executable, parses args, streams fixtures
- [ ] 6 fixture .jsonl files committed
- [ ] docker-compose.test.yml + Dockerfile.test-runner
- [ ] conftest.py compose fixture with healthz wait
- [ ] Admin-key provisioner fixture
- [ ] OpenAI Python client factory fixture
- [ ] All 13 Python SDK test cases
- [ ] Node SDK test_node_sdk package + 13 Vitest cases
- [ ] CI workflow `.github/workflows/compat.yml` green
- [ ] Coverage ≥ 75% gate enforced on key modules
- [ ] Raw-bytes test for `data: [DONE]` chat-completions terminator
- [ ] README local-run instructions
- [ ] No file > 200 LOC

## Success Criteria
- `make test-compat` → all 26 cases (13 Python + 13 Node) pass on local Docker.
- CI compat workflow green on a clean main branch (after phases 0–8 merged).
- Coverage report ≥ 75% for `src/gateway/*`, `src/codex/*`, `src/workers/*`.
- Mock codex matches researcher-01 schema exactly: if a real codex 0.125.0 run produces an event type not in fixtures, dev sees test gap → adds fixture (process check, not auto).
- Test stack readiness < 30s cold; full suite < 5min in CI.
- Raw `data: [DONE]\n\n` byte-equal terminator confirmed for chat completions stream.
- Responses API event ordering verified: `response.created` → `output_item.added` → `content_part.added` → `output_text.delta+` → `output_text.done` → `completed`.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Mock codex drifts from real codex schema | M | HIGH | Periodic real-codex smoke job (weekly cron in CI, ignored on PRs); diff fixtures against `codex exec --json` capture |
| OpenAI SDK version bump breaks tests | M | M | Pin minor versions in pyproject + package.json; renovate-bot PR triages bumps |
| Compose flakiness in CI | M | M | `--wait` flag + healthz polling; retry once on infra-error class |
| Node container heavy (slows CI) | L | L | Cache pnpm store across runs; alpine base |
| Coverage gate blocks legitimate refactor | L | L | Gate only on key modules; doc-only PRs allowlisted |
| Fixture file gets out of sync with parser | M | M | Schema validator on every fixture: load + assert parser handles each line |
| Raw-bytes test fragile to whitespace | L | M | Compare canonical normalized form; document expected bytes precisely |

## Security Considerations
- Test admin token (`test-admin-token`) used ONLY in test compose; production uses cryptographically random `ADMIN_TOKEN`.
- Mock codex never reaches network; all data from local fixture files (no SSRF risk in tests).
- Fixture files contain NO secrets; reviewed for redaction before commit.
- CI uses ephemeral Postgres/Redis volumes; no persisted test data.

## Real-Codex Drift Cron (LOCKED v1)

`tests/fixtures/canned-prompts.json` — array of 5–10 deterministic prompts exercising distinct codex code paths:
1. plain text reply ("ECHO: hello")
2. multi-paragraph response (forces multiple `agent_message` items)
3. reasoning-eligible prompt (codex emits `reasoning` item)
4. command_execution path ("run `ls /tmp`")
5. file_change path ("create file /tmp/x.txt with content y")
6. sandbox-violation negative case ("read /etc/passwd" → expect SANDBOX_VIOLATION error event)
7. long output (>10k chars, exercises chunking)

`.github/workflows/compat-real-codex.yml` (new file):
```yaml
name: real-codex-drift
on:
  schedule: [{cron: "0 3 * * 0"}]  # Sunday 03:00 UTC
  workflow_dispatch: {}
jobs:
  smoke:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - run: npm i -g @openai/codex@latest && codex --version
      - run: |
          # Restore ChatGPT auth from encrypted secret
          mkdir -p ~/.codex
          echo "${{ secrets.CODEX_AUTH_JSON_AGE }}" \
            | age -d -i <(echo "${{ secrets.AGE_KEY }}") \
            > ~/.codex/auth.json
          chmod 600 ~/.codex/auth.json
      - run: pip install -r requirements-test.txt
      - run: pytest tests/compat/test_real_codex_drift.py -v
      - if: failure()
        uses: actions/github-script@v7
        with:
          script: |
            github.rest.issues.create({
              owner: context.repo.owner, repo: context.repo.repo,
              title: `[drift] real-codex smoke failed ${new Date().toISOString()}`,
              labels: ['drift', 'priority/high'],
              body: 'Weekly real-codex smoke failed. Likely JSONL schema drift in @openai/codex@latest vs pinned 0.125.0. Triage:\n1. Check codex changelog\n2. If breaking: pin stays, document upgrade-blocking issue\n3. If non-breaking: bump pin in next sprint\n\nLogs: ${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}'
            })
```

`tests/compat/test_real_codex_drift.py` (new file): for each canned prompt, run real `codex exec --json` against the prompt, parse via `src/codex/jsonl_parser.py`, assert no parse errors AND that key event types present. Output diff if structure changes.

Failure triage runbook entry added to phase-10 §Runbook (operation #11 "Triage real-codex drift alert").

## Next Steps
- Phase 10 deploy workflow runs compat suite (mock-codex) as pre-deploy gate.
- Phase 10 schedules `compat-real-codex.yml` cron.
- Future phases (v1.1) extend fixtures as new codex features land.
