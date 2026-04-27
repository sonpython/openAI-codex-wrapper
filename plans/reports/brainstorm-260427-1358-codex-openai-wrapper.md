# Brainstorm Report: Codex CLI OpenAI-Compatible Wrapper (Production)

**Date:** 2026-04-27 13:58 GMT+7
**Source design doc:** `/Users/michaelphan/Downloads/service-wrapper-codex-cli-openai-standard.pdf`
**Mode:** Production-grade, full OpenAI standard compatibility (text-only scope)

---

## 1. Problem Statement

Build production-grade service wrapping Codex CLI (`codex exec --json`), exposing OpenAI-compatible HTTP API so any OpenAI SDK/client works against it via `base_url=https://wrapper/v1`. Multi-tenant, observable, rate-limited, isolated.

## 2. Locked-in Decisions (from interview)

| Area | Decision | Rationale |
|---|---|---|
| Endpoint scope | Core text-only: `/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/codex/jobs` | KISS; covers 95% SDK use cases. Skip assistants/threads/embeddings/audio/images/fine-tuning. |
| Stack | Python FastAPI | Matches PDF skeleton, async fits subprocess streaming, fast dev velocity. |
| Wrapper auth | API key per user (Bearer) → Postgres-backed lookup | Standard SaaS pattern, easy bill/quota. |
| Codex auth | ChatGPT login (`codex login`) — **accepted risk** | User priority: avoid OpenAI API token cost. See §7 risks. |
| Sandbox | Codex built-in `--sandbox workspace-write` (Landlock/seccomp on Linux, Seatbelt on macOS) + per-job ephemeral workspace dir | Lighter than Docker-per-job, sufficient with cgroups & process isolation. |
| Deploy | Docker Compose on VM | Single-node start, scale vertical, < 1k user. K8s migration later if needed. |
| Queue | Redis + **Arq** (asyncio-native) | Native to FastAPI async, lighter than Celery. |
| State | Postgres + Redis | Postgres: users/keys/jobs/audit. Redis: rate-limit + queue + cache. |
| Observability | structlog JSON + Prometheus + OpenTelemetry | Logs+metrics+traces baseline. Sentry deferred. |
| Chat compat depth | Text + SSE streaming only | No tools/function-calling/vision/n>1/logprobs. Codex CLI doesn't expose them; faking = brittle. |
| /v1/codex/jobs scope | MVP: `{repo_url, branch, task, mode}` → clone, run, return diff | Enough for review/fix workflows. Auto-PR & webhook deferred. |
| Rate limit | Multi-tier: RPM + TPM + concurrent + monthly quota; OpenAI-style `X-RateLimit-*` headers | Match OpenAI client expectations. |
| Workspace lifecycle | Ephemeral, cleanup post-response | Disk-safe, security default. |
| Codex CLI version | Pin `@openai/codex@MAJOR.MINOR.PATCH` in Dockerfile | Reproducible; JSONL schema may break across versions. |

## 3. Architecture

```
                  ┌────────────────────────────────────────┐
Client ── TLS ──► │ Caddy/Traefik (reverse proxy + ACME)   │
(OpenAI SDK)      └──────────────┬─────────────────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────────────────┐
                  │ FastAPI Gateway (uvicorn workers)        │
                  │  middleware: auth → rate-limit → trace   │
                  │  routes:                                 │
                  │   GET  /v1/models                        │
                  │   POST /v1/chat/completions  (SSE)       │
                  │   POST /v1/responses         (SSE)       │
                  │   POST /v1/codex/jobs        (enqueue)   │
                  │   GET  /v1/codex/jobs/{id}               │
                  │   GET  /v1/codex/jobs/{id}/events        │
                  └──────────────┬───────────────────────────┘
                                 │
        ┌────────── inline SSE ──┼──── enqueue ─────────┐
        │ (chat/responses)       │  (jobs)              │
        ▼                        ▼                      ▼
  ┌─────────────┐         ┌──────────────┐      ┌──────────────┐
  │ codex.runner│         │ Redis queue  │      │ Postgres     │
  │ subprocess  │         │ (arq)        │      │ users        │
  │ → JSONL     │         └──────┬───────┘      │ api_keys     │
  │ → SSE chunk │                │              │ jobs         │
  └──────┬──────┘                ▼              │ audit_log    │
         │                ┌──────────────┐      │ usage_counter│
         │                │ Arq worker   │      └──────────────┘
         │                │ (1+ replicas)│
         │                │ codex exec   │
         │                │ → diff       │
         │                └──────┬───────┘
         │                       │
         └──────── ephemeral ────┴── /workspaces/{job_id} (tmpfs)

Shared volumes:
  /codex-auth  (read-only mount of ~/.codex from logged-in admin)
  /var/log     (JSON logs → Loki/CloudWatch agent)

Sidecar/exporters:
  Prometheus scrape :9090/metrics
  OTEL collector → Tempo/Jaeger
```

## 4. Project Structure

```
codex-wrapper/
├── docker-compose.yml           # gateway, worker, postgres, redis, caddy, otel-collector
├── Dockerfile.gateway           # python 3.12-slim + node + codex CLI
├── Dockerfile.worker            # same base + git
├── Caddyfile                    # TLS + reverse proxy
├── pyproject.toml               # uv project
├── alembic.ini
├── src/
│   ├── settings.py              # pydantic-settings (env)
│   ├── gateway/
│   │   ├── app.py               # FastAPI factory
│   │   ├── routes/
│   │   │   ├── models.py
│   │   │   ├── chat.py          # /v1/chat/completions
│   │   │   ├── responses.py     # /v1/responses
│   │   │   └── jobs.py          # /v1/codex/jobs*
│   │   ├── middleware/
│   │   │   ├── auth.py          # bearer key → user
│   │   │   ├── rate_limit.py    # redis sliding window
│   │   │   ├── usage.py         # TPM + monthly quota
│   │   │   └── tracing.py       # otel hooks
│   │   └── schemas/
│   │       ├── openai_chat.py   # ChatCompletionRequest etc
│   │       └── openai_responses.py
│   ├── codex/
│   │   ├── runner.py            # subprocess + asyncio
│   │   ├── jsonl_parser.py      # event → openai chunk
│   │   ├── workspace.py         # mkdir/cleanup/path-safety
│   │   └── auth_session.py      # ~/.codex mount check + healthcheck
│   ├── workers/
│   │   ├── arq_worker.py        # entrypoint
│   │   └── job_handlers.py      # clone, run, diff
│   ├── db/
│   │   ├── engine.py
│   │   ├── models.py            # sqlalchemy 2.0 declarative
│   │   ├── crud/
│   │   └── migrations/          # alembic
│   └── observability/
│       ├── logging.py           # structlog config
│       ├── metrics.py           # prometheus_client
│       └── tracing.py           # opentelemetry setup
├── tests/
│   ├── unit/
│   ├── integration/             # docker-compose test stack
│   └── compat/                  # OpenAI SDK smoke tests
├── docs/
│   ├── project-overview-pdr.md
│   ├── system-architecture.md
│   ├── code-standards.md
│   ├── codebase-summary.md
│   └── deployment-guide.md
└── plans/
```

Convention: kebab-case files, < 200 LOC per file, type hints + structlog logger per module.

## 5. Postgres Schema (sketch)

```sql
users(id uuid pk, email text uniq, created_at);
api_keys(id uuid pk, user_id fk, key_hash text uniq,            -- argon2id
         name text, last_used_at, revoked_at, created_at,
         tier text);                                            -- free/pro/ent
plans(tier text pk, rpm int, tpm int, concurrent int, monthly_tokens bigint);
jobs(id uuid pk, user_id fk, status text,                       -- queued/running/succeeded/failed/cancelled
     repo_url text, branch text, task text, mode text,
     workspace_path text, exit_code int,
     diff_blob text, summary text, files_changed jsonb,
     stdout_log_url text, stderr_log_url text,
     enqueued_at, started_at, finished_at);
audit_log(id bigserial pk, user_id fk, api_key_id fk,
          endpoint text, request_id text,
          codex_cmd text[], prompt_hash text,
          input_tokens int, output_tokens int,
          duration_ms int, status_code int, created_at);
usage_counter(user_id fk, period date,                          -- daily roll-up
              requests int, input_tokens bigint, output_tokens bigint,
              pk(user_id, period));
```

## 6. JSONL → OpenAI Mapping

| Codex event | OpenAI chunk |
|---|---|
| `thread.started` | role: assistant first chunk (empty delta) |
| `item.completed` type=`agent_message` | `delta.content` chunk |
| `item.completed` type=`reasoning` | optional `reasoning_content` (Responses API only); skip in chat-completions |
| `item.completed` type=`tool_use` | log; skip in chat-completions text-only mode |
| `turn.completed` | `finish_reason: stop` + `[DONE]` |
| `error` | error chunk + close stream |

For non-stream: collect all `agent_message` text → join → return single completion.

## 7. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **ChatGPT account ban for API resell** | HIGH | (a) Privacy policy + ToS disclosure to users. (b) Multi-account pool with rotation (fallback). (c) Plan migration path to OpenAI API key when business proves. |
| **Session token expiry on `~/.codex`** | MED | Healthcheck cron `codex auth status` every 5 min; alert + auto-disable wrapper if expired. |
| **Account-level rate limit cascade** | HIGH | Per-account `concurrent` cap = 80% of ChatGPT limit; queue overflow returns 429. Multi-account pool. |
| **Codex JSONL schema breakage** | MED | Pin `@openai/codex@x.y.z`; integration test checks event types on every CI; canary worker (future). |
| **Workspace path traversal** | MED | All paths under `/workspaces/{job_id}`; `realpath` validate before write; deny symlinks. |
| **Long-running job DoS** | MED | Hard timeout per job (default 15 min, configurable per tier); SIGTERM → SIGKILL escalation; concurrent cap per key. |
| **Secret leak in logs** | HIGH | structlog redactor for `Authorization`, `OPENAI_API_KEY`, `CODEX_API_KEY`; dedicated reviewer in CI. |
| **API key in DB** | HIGH | argon2id hash; only prefix shown to user post-creation; rotation endpoint. |
| **Subprocess zombie / leak** | MED | `asyncio.create_subprocess_exec` with explicit `await process.wait()`; container restart policy; cgroup memory cap. |
| **Multi-user same workspace** | LOW | Workspace dir keyed by job_id (uuid); never reused. |

## 8. Implementation Phases (proposed)

| Phase | Deliverable | Est. effort |
|---|---|---|
| 0. Bootstrap | Repo skeleton, docker-compose, alembic init, structlog, settings | S |
| 1. Auth + models | `/v1/models`, bearer middleware, api_keys CRUD | S |
| 2. Codex runner | subprocess wrapper, JSONL parser, workspace mgmt, unit tests | M |
| 3. Chat completions | `/v1/chat/completions` sync + SSE stream | M |
| 4. Responses API | `/v1/responses` (Responses event shape) | M |
| 5. Jobs API + Arq | `/v1/codex/jobs*`, clone/run/diff worker | L |
| 6. Rate limit + quota | Redis sliding window, monthly counters, OpenAI headers | M |
| 7. Observability | Prometheus metrics, OTEL traces, dashboards | M |
| 8. Hardening | Timeout/cancel, workspace cleanup, secret rotation, audit log | M |
| 9. Compat tests | OpenAI SDK smoke suite (Python + Node) | S |
| 10. Deploy hardening | TLS via Caddy, backups, log shipping, runbook | M |

## 9. Success Metrics

- **Compat:** OpenAI Python SDK + Node SDK both pass smoke suite (chat sync, chat stream, responses, models list).
- **Latency:** p95 first-token < 2s on `/chat/completions` stream.
- **Reliability:** 99.5% uptime / 30d (excluding planned ChatGPT-session refresh windows).
- **Isolation:** zero cross-job workspace leak (integration test asserts).
- **Rate-limit accuracy:** within ±1% of declared limits at 100 req/s load.
- **Security:** no secret in logs (CI grep); api_key never returned post-creation.

## 10. Anti-patterns Locked Out

- ❌ No raw shell endpoint
- ❌ No client-supplied `cwd` or `--sandbox=danger-full-access`
- ❌ No shared workspace across users
- ❌ No mocked/faked tool_calls (text-only by design)
- ❌ No persistent workspace by default
- ❌ No `--no-verify` git, no force push from worker

## 11. Final Locked Decisions (round 2)

| Question | Decision |
|---|---|
| Multi-account ChatGPT pool | **Defer to v1.1** — ship single-account; add pool when 429 pain hits. |
| Cancellation API | **In v1** — `DELETE /v1/codex/jobs/{id}` → SIGTERM/SIGKILL escalation; status=`cancelled`. |
| GitHub auth for /jobs | **Public repos only v1**; PAT/installation defer v1.1. Reject private clone with 422. |
| /v1/responses taxonomy | **Exact OpenAI match** — `response.created`, `response.in_progress`, `response.output_text.delta`, `response.completed`, etc. |
| Tier values (placeholder, env-tunable) | free: 20 RPM / 20k TPM / 100k monthly; pro: 200 / 200k / 2M; ent: 2000 / 2M / 20M. |
| Billing integration | **Defer v1.1** — `usage_counter` table is scaffolding only. |
| Webhook callbacks | **Defer v1.1** — polling `GET /jobs/{id}` enough for MVP. |

## 12. Updated Phase List (post-round-2)

| Phase | Deliverable |
|---|---|
| 0 | Bootstrap: repo skeleton, docker-compose, alembic, structlog, settings |
| 1 | Auth: bearer middleware, api_keys CRUD, `/v1/models` |
| 2 | Codex runner: subprocess + JSONL parser + workspace mgmt + healthcheck on `~/.codex` |
| 3 | `/v1/chat/completions` sync + SSE |
| 4 | `/v1/responses` (exact OpenAI event taxonomy) |
| 5 | Arq worker + `/v1/codex/jobs` (public clone, run, diff) + `DELETE /jobs/{id}` cancel |
| 6 | Multi-tier rate limit (RPM+TPM+concurrent+monthly) + OpenAI-style headers |
| 7 | Observability: Prometheus metrics + OTEL traces + structlog redaction |
| 8 | Hardening: timeout, workspace cleanup, secret rotation, audit log |
| 9 | OpenAI SDK compat smoke tests (Python + Node) |
| 10 | Deploy hardening: Caddy TLS, backup, log shipping, runbook |
