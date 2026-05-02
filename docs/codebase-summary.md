# Codebase Summary

**Repository:** codex-wrapper (Codex CLI OpenAI-compatible wrapper)  
**Language:** Python 3.12  
**Lines of Code:** ~9,500 src/ + tests  
**Modules:** 81 source files  
**Tests:** 615 unit tests (pytest + pytest-asyncio)

---

## Source Code Tree

```
src/
├── __init__.py
├── settings.py                      (10k) pydantic-settings, env vars, pool sizing
├── redis_client.py                  Redis async client singleton
│
├── admin_ui/                        Web dashboard + management UI (HTMX/Jinja2)
│   ├── __init__.py
│   ├── auth.py                      Cookie-session HMAC signing, Redis-backed sessions
│   ├── prom_client.py               Prometheus query helper, 5s server-side cache
│   ├── routes.py                    Main router (login, dashboard, pages)
│   ├── {keys,tiers,jobs,audit,users}_page_routes.py — Per-page routers
│   ├── templates_env.py             Jinja2 environment singleton
│   ├── templates/                   HTML templates (dashboard, modals, forms)
│   └── static/                      CSS/JS assets (Tailwind CDN, Chart.js CDN)
│
├── auth/                            API key auth (bearer tokens)
│   ├── __init__.py
│   ├── bearer.py                    Extract + validate bearer token from headers
│   ├── errors.py                    Auth exceptions
│   └── hashing.py                   argon2id hash + verify (prefix shown once)
│
├── chat/                            Chat completions sync + streaming
│   ├── __init__.py
│   ├── id_factory.py                Generate chat_id (timestamp-based)
│   ├── prompt_builder.py            Format messages → Codex prompt (multi-turn with tool_calls history)
│   ├── sync_handler.py              Sync chat endpoint (blocking + tool_calls branch)
│   ├── stream_handler.py            SSE streaming chat endpoint
│   ├── tool_calling.py              (NEW) Tool-calling synthesis via prompt-engineering
│   └── usage_estimator.py           Estimate input/output tokens (heuristic)
│
├── codex/                           Codex CLI subprocess + event handling
│   ├── __init__.py
│   ├── runner.py                    (225 LOC) codex exec --json subprocess manager
│   ├── jsonl_parser.py              Parse JSONL event stream from codex stdout
│   ├── workspace.py                 Ephemeral job workspace (tmpfs, cleanup)
│   ├── auth_session.py              ChatGPT login session mgmt (readiness)
│   ├── events.py                    Event class hierarchy (codex domain events)
│   ├── exceptions.py                Codex-specific exceptions
│   └── stderr_archive.py            Preserve subprocess stderr for debugging
│
├── db/                              Database layer (Postgres + migrations)
│   ├── __init__.py
│   ├── engine.py                    SQLAlchemy 2.0 engine (dual pool: main 20/10, bg 3/0)
│   ├── models.py                    ORM models (users, api_keys, jobs, plans, usage_counter)
│   ├── models_audit_log.py          Audit log ORM model
│   ├── models_usage_daily.py        Daily usage aggregates (NEW) (user_id, api_key_id, date composite PK)
│   ├── crud/                        Data access layer
│   │   ├── __init__.py
│   │   ├── users.py                 Create, read, list users
│   │   ├── api_keys.py              Create, read, rotate, revoke API keys
│   │   ├── jobs.py                  Create, read, update, delete jobs; capture api_key_id + token counts
│   │   ├── audit_log.py             Log API calls
│   │   ├── plans.py                 Read plan tiers (rate limit quotas)
│   │   ├── usage_counter.py         Track monthly usage per user
│   │   └── usage_daily.py           Atomic upsert daily aggregates (requests + tokens)
│   └── migrations/                  Alembic migrations
│       ├── env.py                   Migration config
│       └── versions/
│           ├── 20260427_0001_init.py
│           ├── 20260427_0002_users_and_api_keys.py
│           ├── 20260427_0003_jobs_table.py
│           ├── 20260427_0004_plans_seed.py
│           └── 20260427_0006_audit_log.py
│
├── gateway/                         FastAPI HTTP layer
│   ├── __init__.py
│   ├── app.py                       FastAPI factory, lifespan, startup/shutdown
│   ├── health.py                    /healthz (basic), /readyz (Postgres + Redis)
│   ├── sse_helpers.py               Keepalive heartbeat for SSE streams
│   ├── ssrf_transport.py            URL validation + safe HTTP client
│   ├── rate_limit_errors.py         Rate-limit exception classes
│   ├── rate_limit_reset_format.py   Format Retry-After header
│   ├── rate_limit_token_estimator.py (227 LOC) Estimate tokens in requests
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── request_id.py            Generate + propagate request IDs (outermost)
│   │   ├── observability.py         Structlog context, timing, errors
│   │   ├── edge_ip_limiter.py       Rate-limit by IP (intra-mesh)
│   │   ├── auth.py                  Bearer token → user lookup
│   │   ├── rate_limit.py            (384 LOC) Multi-tier rate limit (RPM, TPM, concurrent, monthly)
│   │   ├── timeout.py               Per-request timeout enforcement
│   │   └── usage_tracking.py        Track API usage (requests, tokens, errors)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── models.py                GET /v1/models
│   │   ├── chat_completions.py      POST /v1/chat/completions (sync + SSE)
│   │   ├── responses.py             POST /v1/responses (sync + SSE, 50+ events)
│   │   ├── jobs.py                  (289 LOC) POST/GET/DELETE /v1/codex/jobs*
│   │   ├── admin_api_keys.py        POST /v1/admin/api-keys, PUT rotate
│   │   ├── admin_codex_stderr.py    GET /v1/codex/stderr/{job_id}
│   │   ├── admin_tiers.py           (NEW) GET/PUT /admin/tiers (tier editor with cache invalidation)
│   │   ├── admin_jobs.py            (NEW) GET /admin/jobs (paginated job list)
│   │   ├── admin_audit.py           (NEW) GET /admin/audit (audit log viewer)
│   │   ├── admin_users.py           (NEW) GET /admin/users, GET /admin/users/{user_id}/keys
│   │   └── admin_usage.py           (NEW) GET /admin/usage/summary, GET /admin/usage/by-key/{key_id}
│   └── schemas/
│       ├── __init__.py
│       ├── chat_request.py          Request/response schemas for chat endpoint
│       ├── chat_response.py
│       ├── responses_request.py     Request/response schemas for responses API
│       ├── responses_object.py
│       └── jobs.py                  Request/response schemas for jobs API
│
├── responses/                       Responses API (50+ event types)
│   ├── __init__.py
│   ├── events_emitter.py            (270 LOC) Emit structured events (completion, chunk, etc.)
│   ├── responses_helpers.py         Format responses API payloads
│   ├── sync_handler.py              Responses sync endpoint
│   └── stream_handler.py            Responses SSE endpoint
│
├── observability/                   Logging, metrics, tracing, alerting
│   ├── __init__.py
│   ├── logging.py                   structlog config + redaction processor
│   ├── metrics.py                   Prometheus instruments (16 total)
│   ├── tracing.py                   OpenTelemetry OTLP initialization
│   └── alert_webhooks.py            (NEW) Prometheus alertmanager webhook handlers
│
├── workers/                         Arq background tasks + job lifecycle
│   ├── __init__.py
│   ├── arq_worker.py                Arq worker entrypoint (async job runner)
│   ├── job_handlers.py              Task handlers (clone, run, diff, publish events, usage_daily upsert)
│   ├── event_publisher.py           (NEW) Publish job events to Redis pub/sub
│   ├── git_clone.py                 Clone repo to ephemeral workspace
│   ├── git_diff.py                  (NEW) Generate diff after codex run
│   ├── janitor.py                   Cleanup stale workspaces + dead Redis entries
│   └── repo_url_head_check.py       Validate repo URL before clone (SSRF guard)
│
└── infra/                           Infrastructure as code (Redis Lua scripts)
    ├── __init__.py
    └── redis_lua/                   (NEW) Lua scripts for rate-limit
        ├── __init__.py
        └── (Lua scripts for sliding windows, rate-limit ops)
```

---

## Module Responsibilities

| Module | Purpose | Key Classes / Functions |
|--------|---------|------------------------|
| `admin_ui.auth` | Cookie-session HMAC sign/verify, Redis session CRUD | `sign_session()`, `verify_session()`, `create_session()` |
| `admin_ui.prom_client` | Prometheus query helper, 5s server-side cache | `query_prometheus()`, `get_dashboard_metrics()` |
| `admin_ui.routes` | Main admin UI router (login, dashboard dispatch) | `login_page()`, `dashboard_page()` |
| `auth.bearer` | Extract bearer token from Authorization header | `extract_bearer_token()`, `validate_api_key()` |
| `auth.hashing` | argon2id hash/verify for API keys | `hash_key()`, `verify_key()` |
| `chat.prompt_builder` | Format SDK messages into Codex prompt (multi-turn with tool_calls) | `build_prompt_for_codex()` |
| `chat.stream_handler` | SSE streaming chat endpoint | `stream_chat_completions()` |
| `chat.sync_handler` | Blocking chat endpoint (gather full response + tool_calls branch) | `sync_chat_completions()` |
| `chat.tool_calling` | Tool-calling synthesis via prompt-engineering | `format_tools_prompt()`, `parse_tool_calls_response()` |
| `codex.runner` | Subprocess manager for `codex exec --json` | `CodexRunner.run()`, `CodexRunner.cancel()` |
| `codex.jsonl_parser` | Parse newline-delimited JSON from stdout | `JSONLParser.parse_event()` |
| `codex.workspace` | Ephemeral tmpfs directory per job | `Workspace.create()`, `Workspace.cleanup()` |
| `codex.events` | Domain event classes (Input, Output, Error, etc.) | `CodexEvent`, subclasses per event type |
| `db.engine` | SQLAlchemy dual-pool engine config | `get_engine()`, `main_session()`, `bg_session()` |
| `db.crud.api_keys` | CRUD for API keys (create, rotate, revoke) | `create_api_key()`, `rotate_key()`, `validate_key()` |
| `db.crud.jobs` | CRUD for jobs (enqueue, poll, cancel) | `create_job()`, `get_job()`, `update_job_status()`, `update_token_counts()` |
| `db.crud.usage_daily` | Atomic upsert daily usage aggregates | `upsert()` (pg_insert.on_conflict_do_update) |
| `gateway.app` | FastAPI factory + middleware stack | `create_app()`, lifespan handlers |
| `gateway.middleware.auth` | Bearer token → user lookup + request.user | `auth_middleware()` |
| `gateway.middleware.rate_limit` | Multi-tier rate limit enforcement | `RateLimitMiddleware`, RPM/TPM/concurrent logic |
| `gateway.middleware.usage_tracking` | Async audit log + usage_daily writes | `track_usage()`, background task queue |
| `gateway.routes.chat_completions` | POST /v1/chat/completions (sync + SSE, tool_calls support) | `chat_completions_sync()`, `chat_completions_stream()` |
| `gateway.routes.jobs` | POST/GET/DELETE /v1/codex/jobs* | `enqueue_job()`, `get_job()`, `cancel_job()`, `stream_events()` |
| `observability.logging` | structlog setup + secret redaction | `configure_logging()`, `RedactionProcessor` |
| `observability.metrics` | Prometheus instrument definitions + reporting | 16 instruments (latency, errors, queue depth, etc.) |
| `observability.alert_webhooks` | Alertmanager webhook handlers (NEW) | `handle_alert_firing()`, `handle_alert_resolved()` |
| `responses.events_emitter` | Emit 50+ Responses API event types | `ResponsesEmitter.chunk()`, `.end()`, `.error()` |
| `workers.job_handlers` | Arq task handlers (clone, run, diff) | `run_codex_job()`, `publish_job_event()` |
| `workers.event_publisher` | Publish job events to Redis pub/sub (NEW) | `publish_job_event()`, `subscribe_to_events()` |
| `workers.git_clone` | Clone repo to workspace | `clone_repo_to_workspace()` |
| `workers.git_diff` | Generate diff after codex run (NEW) | `generate_diff()` |
| `workers.janitor` | Cleanup stale workspaces + Redis debris | `cleanup_stale_workspaces()`, `cleanup_dead_pubsub_entries()` |
| `infra.redis_lua` | Redis Lua scripts for rate-limiting (NEW) | Sliding window RPM/TPM scripts |

---

## Key Entry Points

### Gateway (HTTP API)

**Startup:**
```
uvicorn src.gateway.app:create_app --host 0.0.0.0 --port 8000
```

- `src/gateway/app.py` — FastAPI factory; sets up middleware stack + lifespan
- Middleware order: RequestID → Observability → EdgeIPLimiter → Auth → RateLimit → UsageTracking → Timeout
- Routes registered in `gateway.routes.*`

### Worker (Background Jobs)

**Startup:**
```
arq src.workers.arq_worker.WorkerSettings
```

- `src/workers/arq_worker.py` — Arq worker config + function registry
- Task handlers in `src/workers/job_handlers.py`
- Consumes Redis queue; publishes events via Redis pub/sub

### Database

**Migrations:**
```
alembic upgrade head
```

- `src/db/migrations/` — Alembic versioned migrations
- `src/db/models.py` — SQLAlchemy ORM models (declarative)
- `src/db/crud/*` — Data access layer (CRUD operations per domain)

---

## Dependency Graph

```
gateway (HTTP layer)
  └─ auth.bearer (token extraction)
  └─ auth.hashing (API key validation)
  └─ chat.* (message formatting, handler logic)
  └─ responses.* (events, formatting)
  └─ db.crud.* (user, key, job lookups)
  └─ observability.* (logging, metrics, tracing)
  └─ redis_client (rate-limit state, pub/sub)
  
workers (background tasks)
  └─ codex.runner (subprocess execution)
  └─ codex.jsonl_parser (event parsing)
  └─ codex.workspace (tmpfs cleanup)
  └─ codex.events (event types)
  └─ db.crud.jobs (job status updates)
  └─ responses.events_emitter (event formatting)
  └─ redis_client (pub/sub, event publishing)
  └─ observability.* (task logging)

db (persistence)
  └─ db.models (ORM definitions)
  └─ db.migrations (Alembic versioning)

observability (cross-cutting)
  └─ redis_client (metrics export, alerts)
  └─ None (no upstream deps; imported by all layers)
```

---

## Code Statistics

| Category | Count |
|----------|-------|
| Python source files | 95+ (admin_ui, usage_daily, admin data routes) |
| Total lines of code (src/) | ~10,500+ |
| Unit test files | 65 |
| Unit tests | 615 |
| Classes | ~90+ |
| Functions | ~200+ |
| Prometheus metrics | 16 |
| OpenAI event types | 50+ |
| DB tables | 7 (users, api_keys, jobs, plans, audit_log, usage_daily, usage_counter) |
| Redis namespaces | 6 (rate-limit keys, queue, pub/sub, cancel flags, cache, sessions) |
| Docker containers | 8 (gateway, worker, postgres, redis, caddy, prometheus, grafana, otel-collector) |
| Grafana dashboards | 3 (system-overview, api-endpoints, codex-cli) |

---

## Test Layout

```
tests/
├── unit/                            615 unit tests
│   ├── test_auth_*                  Bearer token, argon2id hashing
│   ├── test_chat_*                  Message formatting, sync/stream handlers
│   ├── test_codex_*                 Runner, JSONL parser, workspace, events
│   ├── test_db_*                    CRUD operations, migrations
│   ├── test_gateway_*               Routes, middleware, health checks
│   ├── test_observability_*         Logging redaction, metrics, tracing
│   ├── test_rate_limit_*            RPM, TPM, concurrent, monthly quotas
│   ├── test_responses_*             Events emitter, event taxonomy
│   ├── test_workers_*               Job handlers, git ops, janitor
│   └── test_middleware_*            Middleware ordering, auth flow
├── compat/                          SDK compatibility tests
│   ├── python_sdk_test.py           OpenAI Python SDK (sync + stream)
│   └── node_sdk_test.js             OpenAI Node.js SDK (sync + stream)
└── fixtures/                        Mock data + utilities
    ├── mock_codex/                  Mock Codex CLI (emits test JSONL)
    └── canned_prompts.json          Fixtures for real-codex smoke tests
```

---

## Configuration & Environment

**Settings source:** `src/settings.py` (pydantic-settings)

Key environment variables:
- `DATABASE_URL` — Postgres connection string
- `REDIS_URL` — Redis connection string
- `CODEX_AUTH_JSON_PATH` — Path to `~/.codex/auth.json` (mounted RO)
- `LOG_LEVEL` — structlog level (DEBUG, INFO, WARNING, ERROR)
- `OPENTELEMETRY_ENDPOINT` — OTLP collector HTTP endpoint
- `PROMETHEUS_SCRAPE_INTERVAL` — Prometheus scrape frequency
- See `.env.example` for full list

**Pool sizing (phase-00 red-team C9):**
- Main pool: `pool_size=20, max_overflow=10, pool_timeout=2.0` (request threads)
- Background pool: `pool_size=3, max_overflow=0, pool_timeout=0.5` (fire-and-forget, never starves main)
- Math: 100 RPS × 50ms argon2 = 5 in-flight; 2x for burst = 10-15; main pool headroom to 30 total

---

## Pinned Dependencies

- `@openai/codex@0.125.0` (exact; JSONL schema stability)
- `fastapi==0.100.*`
- `sqlalchemy[asyncio]==2.0.*`
- `asyncpg>=0.28` (Postgres driver)
- `redis[asyncio]>=5.0`
- `arq>=0.25` (Arq queue)
- `argon2-cffi>=24.1`
- `structlog>=24.*`
- `prometheus-client>=0.18`
- `opentelemetry-api>=1.20`
- `opentelemetry-exporter-otlp-proto-http>=0.41b0`
- See `pyproject.toml` for full list

---

## Build & Deploy

**Container images:**
- `Dockerfile.gateway` — Python 3.12-slim + Node + Codex CLI + uvicorn
- `Dockerfile.worker` — Python 3.12-slim + Node + Codex CLI + git + Arq worker

**Orchestration:**
- `docker-compose.yml` — Local dev + prod single-VM deploy
- 5 services: gateway, worker, postgres, redis, caddy

**Reverse proxy:**
- `Caddyfile` — TLS termination, `/v1/*` routing, ACME

---

## Observability Instruments (Prometheus)

| Name | Type | Labels | Purpose |
|------|------|--------|---------|
| `codex_wrapper_request_duration_seconds` | Histogram | method, path, status | HTTP request latency |
| `codex_wrapper_request_errors_total` | Counter | method, path, error_type | Error counts by type |
| `codex_wrapper_response_size_bytes` | Histogram | method, path | Response body sizes |
| `codex_wrapper_rate_limit_headroom_tokens` | Gauge | user_id, plan | TPM quota remaining |
| `codex_wrapper_rate_limit_rejections_total` | Counter | dimension (rpm/tpm/concurrent) | Rate-limit hits |
| `codex_wrapper_job_queue_depth` | Gauge | None | Pending jobs in queue |
| `codex_wrapper_job_duration_seconds` | Histogram | status, repo_type | Job run time |
| `codex_wrapper_workspace_size_bytes` | Gauge | job_id | Ephemeral dir size |
| `codex_wrapper_codex_stdout_events_total` | Counter | event_type | Codex event counts |
| `codex_wrapper_auth_lookup_duration_seconds` | Histogram | cache_hit | API key lookup time |
| `codex_wrapper_database_pool_size` | Gauge | pool_name (main/bg) | DB pool utilization |
| `codex_wrapper_redis_command_duration_seconds` | Histogram | command | Redis op latency |
| `codex_wrapper_sse_active_connections` | Gauge | endpoint | Active SSE streams |
| `codex_wrapper_subscription_lag_seconds` | Gauge | job_id | Pub/sub replay lag |
| `codex_wrapper_audit_log_writes_total` | Counter | table | Audit table inserts |
| `codex_wrapper_monthly_usage_tokens` | Counter | user_id, month | Monthly token usage |

---

## Next Steps for New Developers

1. Read `docs/code-standards.md` — file size, naming, async patterns, error handling
2. Read `docs/system-architecture.md` — middleware stack, data flow, rate-limit model
3. Run `uv sync && pytest tests/unit/ -v` — confirm test suite passes
4. Review `src/gateway/app.py` — understand FastAPI factory + lifespan
5. Review `src/codex/runner.py` — understand subprocess streaming + JSONL parsing
6. Trace a request from `gateway/routes/chat_completions.py` through middleware → `chat.sync_handler` → `codex.runner`

---

**Last Updated:** 2026-05-02 (admin UI, daily usage tracking, Grafana dashboards, Phase 07-10 complete)
