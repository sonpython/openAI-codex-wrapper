# Codex CLI OpenAI-Compatible Wrapper

**Status:** v1 INTERNAL ONLY (ChatGPT login auth mode, production-ready)

Production-grade HTTP API wrapper around `codex exec --json` exposing OpenAI-compatible endpoints for internal team use. Any OpenAI SDK (Python, Node.js, etc.) works via `base_url=https://wrapper.internal/v1`. **Tool-calling (function-calling) support verified with Home Assistant Extended OpenAI Conversation.**

## Features

- **OpenAI-compatible endpoints** — `/v1/models`, `/v1/chat/completions`, `/v1/responses` (50+ event taxonomy)
- **Tool-calling synthesis** — Prompt-engineered function-calling with full JSON schema support (HA EOC multi-turn verified)
- **Codex job API** — `/v1/codex/jobs` with ephemeral sandboxed workspaces per task
- **Multi-tier rate limiting** — RPM / TPM / concurrent / monthly quotas with OpenAI-style headers
- **Bearer auth + admin API** — API key rotation, per-user audit logs
- **Admin UI** — Web dashboard at `/admin/ui` (login with `ADMIN_TOKEN`): API key CRUD, tier editor, per-user usage (30d chart), job inspector, audit viewer, live KPI polling every 5s
- **Daily usage tracking** — Atomic per-key + per-user request/token aggregates, `usage_daily` table with indexes
- **Production observability** — structlog JSON, Prometheus + Grafana dashboards, OpenTelemetry traces, Loki logs, Tempo traces
- **Hardening** — SSRF guard, timeout middleware, stderr archive, workspace path isolation (Landlock/seccomp on Linux)
- **Internal-only scope** — ChatGPT login (no external customers under v1); access gate enforces non-public reachability

## Quick Start

### Prerequisites
- Docker Compose
- Codex CLI ≥ 0.125.0 installed locally (`npm i -g @openai/codex`)
- ChatGPT login session created locally (`codex login --device-auth` → `~/.codex/auth.json`)

### Start Services

```bash
# Copy env template
cp .env.example .env

# Boot stack: gateway, worker, postgres, redis, caddy (TLS), otel-collector
docker compose up -d

# Wait for Postgres readiness
docker compose exec gateway alembic upgrade head

# Verify
curl -k https://localhost/v1/models
```

### Using with OpenAI SDK

**Python:**
```python
from openai import OpenAI
client = OpenAI(
    api_key="sk-your-api-key",
    base_url="https://wrapper.internal/v1"
)
response = client.chat.completions.create(
    model="codex",
    messages=[{"role": "user", "content": "Fix the bug in src/main.py"}]
)
```

**Node.js:**
```javascript
import OpenAI from "openai";
const client = new OpenAI({
  apiKey: "sk-your-api-key",
  baseURL: "https://wrapper.internal/v1",
});
const response = await client.chat.completions.create({
  model: "codex",
  messages: [{ role: "user", content: "Fix the bug in src/main.py" }],
});
```

**Home Assistant Extended OpenAI Conversation:**
```yaml
conversation:
  - platform: extended_openai_conversation
    name: Codex Assistant
    api_type: openai
    api_key: sk-your-api-key
    base_url: https://wrapper.internal/v1
    model: codex
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| `GET` | `/v1/models` | List available models (codex) |
| `POST` | `/v1/chat/completions` | Chat sync + SSE streaming (with tool-calling) |
| `POST` | `/v1/responses` | Responses API sync + SSE events |
| `POST` | `/v1/codex/jobs` | Enqueue repo task (clone, run, diff) |
| `GET` | `/v1/codex/jobs/{id}` | Get job status + summary |
| `DELETE` | `/v1/codex/jobs/{id}` | Cancel running job |
| `GET` | `/v1/codex/jobs/{id}/events` | Stream job events (SSE) |
| `POST` | `/v1/admin/api-keys` | Create API key (admin) |
| `PUT` | `/v1/admin/api-keys/{id}/rotate` | Rotate key (admin) |
| `GET` | `/v1/codex/stderr/{job_id}` | Archive stderr (admin) |

## Compatible Clients

| Client | Status | Notes |
|--------|--------|-------|
| **OpenAI Python SDK** | ✅ Verified | sync + stream paths tested |
| **OpenAI Node.js SDK** | ✅ Verified | sync + stream paths tested |
| **Home Assistant Extended OpenAI Conversation** | ✅ Verified | Multi-turn tool-calling with nested schemas (execute_services) |

## Architecture

```
Client (OpenAI SDK / HA EOC)
    │ TLS (Caddy reverse proxy)
    ▼
FastAPI Gateway (uvicorn)
    │ Middleware: auth → rate-limit → observability
    ├─► Inline SSE: chat-completions (with tool-calling), responses API
    │   └─► codex.runner (subprocess)
    │
    ├─► Admin UI: /admin/ui/* (HTMX + Jinja2, cookie-session auth)
    │
    └─► Arq queue: jobs
        ├─► Redis (queue + rate-limit sliding window)
        └─► Postgres (users, api_keys, jobs, audit_log, usage_daily)
        └─► Arq worker (async background tasks)
            └─► codex exec --json (subprocess)
            └─► /workspaces/{job_id} (ephemeral per task)
```

Observability:
- Logs: structlog JSON → stdout → Promtail → Loki
- Metrics: Prometheus scrape `/_internal/metrics` (16 instruments) → Grafana dashboards
- Traces: OTEL OTLP → Tempo (distributed tracing)
- Dashboards: http://localhost:3001 (Grafana, default `admin/admin`)

## Development Setup

### Install Dependencies

```bash
uv sync
```

### Run Tests

```bash
# Unit tests (615+ tests)
pytest tests/unit/ -v

# With coverage
pytest tests/unit/ --cov=src --cov-report=term-missing

# Specific test file
pytest tests/unit/test_tool_calling.py -v
```

### Code Quality

```bash
# Format + lint
ruff check src/ tests/ --fix
ruff format src/ tests/

# Type checking
mypy src/
```

## Project Structure

```
codex-wrapper/
├── README.md (this file)
├── CLAUDE.md (project rules + workflows)
├── pyproject.toml (uv project, deps, pytest config)
├── docker-compose.yml
├── Dockerfile.gateway
├── Dockerfile.worker
├── alembic.ini
├── .env.example
├── src/
│   ├── settings.py (pydantic-settings, env vars)
│   ├── redis_client.py
│   ├── admin_ui/ (HTMX web UI, cookie-session auth, pages for keys/tiers/jobs/audit/users)
│   ├── auth/ (hashing, bearer, errors)
│   ├── chat/ (prompt_builder, sync/stream handlers, tool_calling synthesis)
│   ├── codex/ (runner, jsonl_parser, workspace, exceptions, events)
│   ├── db/ (engine, models, crud, migrations, usage_daily model)
│   ├── gateway/ (FastAPI app, routes, middleware, schemas, admin_* data endpoints)
│   ├── observability/ (logging, metrics, tracing, alert webhooks)
│   ├── responses/ (events_emitter, handlers, helpers)
│   ├── workers/ (arq worker, job handlers, git ops, janitor, event publisher)
│   └── infra/ (Redis Lua scripts for rate-limit)
├── tests/
│   ├── unit/ (615+ unit tests)
│   ├── compat/ (Python + Node SDK smoke suite + HA EOC)
│   └── fixtures/ (mock-codex, canned prompts)
├── docs/ (detailed documentation)
│   ├── project-overview-pdr.md (vision, scope, risks, metrics)
│   ├── code-standards.md (file size, naming, async patterns)
│   ├── code-standards-patterns.md (detailed patterns: async, DI, middleware, tool-calling schema)
│   ├── codebase-summary.md (module tree, entry points, stats)
│   ├── system-architecture.md (middleware, data flows, storage)
│   ├── system-architecture-modules.md (tool-calling synthesis, HA EOC, detailed data flows, rate-limit model)
│   ├── project-roadmap.md (v1 complete, v1.1 planned, v2 future)
│   ├── deployment-guide.md (VM sizing, first deploy)
│   ├── operations-runbook.md (10 ops, 4 error codes)
│   └── host-hardening.md (UFW, userns-remap, SSH, fail2ban)
└── infra/ (prod deployment)
    ├── Caddyfile.production
    ├── backup/ (pg_dump + age encryption)
    ├── prometheus/ (alerting rules, dashboards)
    ├── loki/ (log retention)
    ├── tempo/ (trace retention)
    ├── grafana/ (datasources)
    ├── promtail/ (log shipper)
    └── otel-collector-config.yaml
```

## Documentation

Start here:

- **[Project Overview & PDR](docs/project-overview-pdr.md)** — Vision, scope, locked decisions, risks, success metrics
- **[Code Standards](docs/code-standards.md)** — File size, naming, async patterns, error handling, testing
- **[Code Standards: Patterns](docs/code-standards-patterns.md)** — Async/await, DI, middleware, tool-calling schema requirements
- **[Codebase Summary](docs/codebase-summary.md)** — Module responsibilities, entry points, dependency graph, stats
- **[System Architecture](docs/system-architecture.md)** — Middleware stack, data flow, storage layout, deployment
- **[System Architecture: Modules](docs/system-architecture-modules.md)** — Tool-calling synthesis, HA EOC compatibility, detailed data flows, rate-limit model, workspace safety
- **[Project Roadmap](docs/project-roadmap.md)** — v1 complete phases, v1.1 planned features, v2 external launch path
- **[Deployment Guide](docs/deployment-guide.md)** — VM sizing, prerequisites, first-deploy walkthrough
- **[Operations Runbook](docs/operations-runbook.md)** — 10 common ops, 4 error codes, troubleshooting
- **[Host Hardening](docs/host-hardening.md)** — UFW, userns-remap, SSH hardening, fail2ban

See also:
- **[Implementation Plan](plans/260427-1358-codex-openai-wrapper/plan.md)** — 11-phase spec with locked decisions, success metrics, top risks
- **[Brainstorm Report](plans/reports/brainstorm-260427-1358-codex-openai-wrapper.md)** — Architecture deep dive, 7 risks, decision rationale

## Key Stats

- **~10,500 LOC** in `src/` (88 Python modules)
- **615+ unit tests** (pytest + pytest-asyncio)
- **16 Prometheus instruments** (latency, errors, queue depth, rate-limit headroom, etc.)
- **27 tool-calling tests** (regression suite for HA EOC nested schemas)
- **v1 INTERNAL ONLY** — no external customers; ChatGPT login auth indefinitely

## Locked Decisions (v1)

| Area | Decision |
|------|----------|
| Stack | Python 3.12 + FastAPI + uvicorn + Arq (asyncio-native queue) |
| Codex auth | ChatGPT login (`~/.codex/auth.json`), single account |
| Wrapper auth | Bearer API key (argon2id hash, prefix shown once) |
| Tool-calling | Prompt-engineered synthesis with full JSON schema inlining (HA EOC compatible) |
| Access gate | Internal-only (Cloudflare Access / Tailscale / IP allowlist) — no public Internet |
| Sandbox | `--sandbox workspace-write` + Landlock/seccomp/Seatbelt per OS |
| Storage | Postgres 16 (durable) + Redis 7 (queue, rate-limit, cache) |
| Streaming | SSE (chat: data-only; responses: 50+ event taxonomy) |
| Rate limit | RPM + TPM + concurrent + monthly (intra-team safety, not billing) |
| Deployment | Docker Compose + Caddy 2 on single VM |
| Observability | structlog → Loki, Prometheus, OTEL → Tempo |

## Out of Scope (v1)

- Vision / multimodal
- Fine-tuning, embeddings, audio
- Public / external customers (v2 decision)
- Multi-account ChatGPT pool
- Webhook callbacks for jobs
- Auto-PR generation

## License

Internal use only. See LICENSE file for details.

---

**Last Updated:** 2026-05-02 (v1 with admin UI, daily usage tracking, Grafana dashboards, Phase 07-10 complete)
