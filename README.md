# Codex CLI OpenAI-Compatible Wrapper

**Status:** Active development, v1 INTERNAL ONLY (ChatGPT login auth mode)

Production-grade HTTP API wrapper around `codex exec --json` exposing OpenAI-compatible endpoints for internal team use. Any OpenAI SDK (Python, Node.js, etc.) works via `base_url=https://wrapper.internal/v1`.

## Features

- **OpenAI-compatible endpoints** — `/v1/models`, `/v1/chat/completions`, `/v1/responses` (50+ event taxonomy)
- **Codex job API** — `/v1/codex/jobs` with ephemeral sandboxed workspaces per task
- **Multi-tier rate limiting** — RPM / TPM / concurrent / monthly quotas with OpenAI-style headers
- **Bearer auth + admin API** — API key rotation, per-user audit logs
- **Production observability** — structlog JSON, Prometheus metrics, OpenTelemetry traces, Loki logs, Tempo traces
- **Hardening** — SSRF guard, timeout middleware, stderr archive, workspace path isolation (Landlock/seccomp on Linux)
- **Internal-only scope** — ChatGPT login (no external customers under v1); access gate enforces non-public reachability

## Quick Start

### Prerequisites
- Docker Compose
- Codex CLI ≥ 0.125.0 installed locally (bootstrap: `npm i -g @openai/codex`)
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
    messages=[{"role": "user", "content": "..."}]
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
  messages: [{ role: "user", content: "..." }],
});
```

## Endpoints

| Method | Path | Summary |
|--------|------|---------|
| `GET` | `/v1/models` | List available models (codex) |
| `POST` | `/v1/chat/completions` | Chat sync + SSE streaming |
| `POST` | `/v1/responses` | Responses API sync + SSE events |
| `POST` | `/v1/codex/jobs` | Enqueue repo task (clone, run, diff) |
| `GET` | `/v1/codex/jobs/{id}` | Get job status + summary |
| `DELETE` | `/v1/codex/jobs/{id}` | Cancel running job |
| `GET` | `/v1/codex/jobs/{id}/events` | Stream job events (SSE) |
| `POST` | `/v1/admin/api-keys` | Create API key (admin) |
| `PUT` | `/v1/admin/api-keys/{id}/rotate` | Rotate key (admin) |
| `GET` | `/v1/codex/stderr/{job_id}` | Archive stderr (admin) |

## Architecture

```
Client (OpenAI SDK)
    │ TLS (Caddy reverse proxy)
    ▼
FastAPI Gateway (uvicorn)
    │ Middleware: auth → rate-limit → observability
    ├─► Inline SSE: chat-completions, responses API
    │   └─► codex.runner (subprocess)
    │
    └─► Arq queue: jobs
        ├─► Redis (queue + rate-limit sliding window)
        └─► Postgres (users, api_keys, jobs, audit_log, usage_counter)
        └─► Arq worker (async background tasks)
            └─► codex exec --json (subprocess)
            └─► /workspaces/{job_id} (ephemeral per task)
```

Observability:
- Logs: structlog JSON → stdout → Promtail → Loki
- Metrics: Prometheus scrape `:9090/_internal/metrics`
- Traces: OTEL OTLP → Tempo

## Development Setup

### Install Dependencies

```bash
uv sync
```

### Run Tests

```bash
# Unit tests (615 tests)
pytest tests/unit/ -v

# With coverage
pytest tests/unit/ --cov=src --cov-report=term-missing
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
│   ├── auth/ (hashing, bearer, errors)
│   ├── chat/ (prompt_builder, sync/stream handlers, usage estimator)
│   ├── codex/ (runner, jsonl_parser, workspace, exceptions, events)
│   ├── db/ (engine, models, crud, migrations)
│   ├── gateway/ (FastAPI app, routes, middleware, schemas)
│   ├── observability/ (logging, metrics, tracing, alerts)
│   ├── responses/ (events_emitter, handlers, helpers)
│   ├── workers/ (arq worker, job handlers, git ops, janitor)
│   └── infra/ (Redis Lua scripts)
├── tests/
│   ├── unit/ (615 unit tests)
│   ├── compat/ (Python + Node SDK smoke suite)
│   └── fixtures/ (mock-codex, canned prompts)
├── docs/ (detailed documentation)
│   ├── project-overview-pdr.md
│   ├── code-standards.md
│   ├── codebase-summary.md
│   ├── system-architecture.md
│   ├── project-roadmap.md
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

- **[Project Overview & PDR](docs/project-overview-pdr.md)** — Vision, scope, locked decisions, risks, success metrics
- **[Code Standards](docs/code-standards.md)** — File size, naming, async patterns, error handling, testing
- **[Codebase Summary](docs/codebase-summary.md)** — Module responsibilities, key entry points, dependency graph
- **[System Architecture](docs/system-architecture.md)** — Middleware stack, data flow, storage layout, auth/rate-limit/sandbox models
- **[Project Roadmap](docs/project-roadmap.md)** — v1 completed phases, v1.1 planned features, v2 external launch path
- **[Deployment Guide](docs/deployment-guide.md)** — VM sizing, prerequisites, first-deploy walkthrough
- **[Operations Runbook](docs/operations-runbook.md)** — 10 common ops, 4 error codes, troubleshooting
- **[Host Hardening](docs/host-hardening.md)** — UFW, userns-remap, SSH hardening, fail2ban

See also:
- **[Implementation Plan](plans/260427-1358-codex-openai-wrapper/plan.md)** — 11-phase spec with locked decisions, success metrics, top risks
- **[Brainstorm Report](plans/reports/brainstorm-260427-1358-codex-openai-wrapper.md)** — Architecture deep dive, 7 risks, decision rationale

## Key Stats

- **~6000 LOC** in `src/` (87 Python modules)
- **615 unit tests** (pytest + pytest-asyncio)
- **81 source modules** (auth, chat, codex, db, gateway, observability, responses, workers)
- **16 Prometheus instruments** (latency, errors, queue depth, rate-limit headroom, etc.)
- **v1 INTERNAL ONLY** — no external customers; ChatGPT login auth indefinitely

## Locked Decisions (v1)

| Area | Decision |
|------|----------|
| Stack | Python 3.12 + FastAPI + uvicorn + Arq (asyncio-native queue) |
| Codex auth | ChatGPT login (`~/.codex/auth.json`), single account |
| Wrapper auth | Bearer API key (argon2id hash, prefix shown once) |
| Access gate | Internal-only (Cloudflare Access / Tailscale / IP allowlist) — no public Internet |
| Sandbox | `--sandbox workspace-write` + Landlock/seccomp/Seatbelt per OS |
| Storage | Postgres 16 (durable) + Redis 7 (queue, rate-limit, cache) |
| Streaming | SSE (chat: data-only; responses: 50+ event taxonomy) |
| Rate limit | RPM + TPM + concurrent + monthly (intra-team safety, not billing) |
| Deployment | Docker Compose + Caddy 2 on single VM |
| Observability | structlog → Loki, Prometheus, OTEL → Tempo |

## Out of Scope (v1)

- Tools / function-calling
- Vision / multimodal
- Fine-tuning, embeddings, audio
- Public / external customers (v2 decision)
- Multi-account ChatGPT pool

## License

Internal use only. See LICENSE file for details.

---

**Last Updated:** 2026-04-27 (v1 INTERNAL ONLY scope locked)
