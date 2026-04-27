# Phase 00: Bootstrap

## Context Links
- Brainstorm: ../reports/brainstorm-260427-1358-codex-openai-wrapper.md (§3 architecture, §4 project structure)
- Codex JSONL: research/researcher-01-codex-jsonl-schema.md (§6 version pinning)
- OpenAI taxonomy: research/researcher-02-openai-event-taxonomy.md
- Project rules: ../../.claude/rules/development-rules.md

## Overview
- Priority: critical
- Status: pending
- Effort: S
- Description: Establish the repository skeleton, container topology, runtime config, structured logging, and database migration scaffolding. This phase produces a buildable, runnable, but feature-empty service whose `/healthz` returns 200 and which can talk to Postgres + Redis. Every subsequent phase plugs into this foundation.

## Red Team Resolutions
- **C1 + C11** — Add `make verify-codex` pre-flight that asserts version `0.125.0`, presence of required flags (`--ephemeral`, `--skip-git-repo-check`, `--json`, `--sandbox`, `--cd`), JSONL-on-stdout still works, and surfaces follow-up TODO if 0.125.0 introduced unix socket transport. Bootstrap fails if any check fails.
- **C9** — Bump main async DB pool to `pool_size=20, max_overflow=10, pool_timeout=2.0` (sized for 100 RPS × 50ms argon2 + worker checkouts). Document the math + add a separate small background-write pool.
- **C8 (prep)** — Define a SECOND, dedicated DB pool for fire-and-forget writes (size 3, `pool_timeout=0.5`); phase-01 consumes it. Background writes can never starve request pool.
- **MM1 (SSE keepalive)** — This phase owns the shared helper location: `src/gateway/sse_helpers.py` (consumed by phases 03/04/05). Helper emits `: keepalive\n\n` comment lines every 15s while upstream is silent — keeps Caddy/CDN idle timers from killing slow streams.

## Key Insights
- Codex CLI v0.125.0 is target (researcher-01 §6); pin EXACT version in Dockerfile to avoid JSONL schema drift.
- File-size rule (< 200 LOC): split FastAPI app factory, settings, lifespan into separate modules from day 0 — retrofitting later is painful.
- structlog redaction processor must be in place from phase 0 so secrets never leak even during early debug (brainstorm §7 secret leak risk).
- `~/.codex` mounted read-only from host into both gateway and worker containers (single shared session, brainstorm §11).
- **Pre-flight `make verify-codex` is non-negotiable**: it stops phase-02 from coding against a flag set that doesn't exist (researcher-01 §6 lists "Unix socket transport" in 0.125.0 changelog — must verify stdout JSONL still default).
- **DB pool sizing math**: 100 RPS × ~50ms p99 (argon2 + auth lookup) = ~5 in-flight; double-booked under burst → 10-15. Background tasks (audit, last_used_at) MUST NOT contend with request pool — they get a separate, smaller pool with a tight timeout that DROPS instead of waiting (data is best-effort).
- SSE keepalive utility centralised in `gateway/sse_helpers.py` so phases 03/04/05 don't each reinvent the heartbeat pattern (DRY).

## Requirements

### Functional
- `docker compose up` boots: gateway, worker, postgres, redis, caddy (TLS dev profile), otel-collector (no-op sink ok this phase).
- `GET /healthz` returns `{"status":"ok"}` HTTP 200.
- `GET /readyz` returns 200 only when Postgres + Redis pingable.
- `alembic upgrade head` applies an empty initial migration to a fresh DB.
- Settings load from env via pydantic-settings; `.env.example` documents every var.
- structlog emits JSON to stdout with `request_id`, `service`, `level`, `event`, `ts`.
- `pyproject.toml` declares all pinned deps; `uv sync` reproduces install.

### Non-Functional
- All Python code files ≤ 200 LOC.
- Type hints + ruff + mypy clean.
- Container image < 800 MB (slim base; codex CLI installed via `npm i -g`).
- Reproducible build: lockfile committed.

## Architecture

```
host:
  ~/.codex/                         (chatgpt session, RO bind into containers)
  /var/lib/codex-wrapper/postgres   (volume)
  /var/lib/codex-wrapper/redis      (volume)

docker compose:
  gateway   :8000  ─┐
  worker    (no port)─┼─ shares image base + ~/.codex RO mount
  postgres  :5432
  redis     :6379
  caddy     :80/443 → gateway
  otel-collector :4317
```

Gateway boot order (lifespan):
1. Load Settings (fail fast if required env missing)
2. Configure structlog
3. Init OTEL tracer (no-op exporter if disabled)
4. Open SQLAlchemy engine + connection check
5. Open Redis pool + ping
6. Mount routers (placeholders this phase)
7. Yield → app ready

## Related Code Files

### To create
- `pyproject.toml` (uv project, deps pinned)
- `uv.lock` (committed)
- `scripts/verify-codex.sh` (pre-flight: version + flag + JSONL-on-stdout assertions; ≤ 80 LOC bash)
- `src/gateway/sse_helpers.py` (shared SSE keepalive util — `keepalive_wrap(iter, interval=15.0)` async wrapper that emits `: keepalive\n\n` when upstream silent; ≤ 80 LOC)
- `Dockerfile.gateway` (python:3.12-slim + nodejs 20 + `npm i -g @openai/codex@0.125.0`)
- `Dockerfile.worker` (same base + `git`)
- `docker-compose.yml`
- `Caddyfile` (dev: localhost; prod overlay later)
- `.env.example`
- `.gitignore`, `.dockerignore`
- `alembic.ini`, `src/db/migrations/env.py`, `src/db/migrations/versions/.gitkeep`
- `src/__init__.py`
- `src/settings.py` (pydantic-settings; ≤ 100 LOC)
- `src/gateway/__init__.py`
- `src/gateway/app.py` (FastAPI factory + lifespan; ≤ 150 LOC)
- `src/gateway/health.py` (`/healthz`, `/readyz` router; ≤ 80 LOC)
- `src/db/__init__.py`
- `src/db/engine.py` (async SQLAlchemy engine + session factory; ≤ 80 LOC)
- `src/db/models.py` (declarative base only this phase; ≤ 50 LOC)
- `src/observability/__init__.py`
- `src/observability/logging.py` (structlog config + redaction processor; ≤ 120 LOC)
- `src/observability/tracing.py` (OTEL setup, no-op fallback; ≤ 80 LOC)
- `src/observability/metrics.py` (prometheus_client registry stub; ≤ 50 LOC)
- `src/redis_client.py` (single redis-py async pool; ≤ 60 LOC)
- `tests/__init__.py`
- `tests/conftest.py` (event loop, test DB url override; ≤ 100 LOC)
- `tests/unit/test_settings.py`
- `tests/unit/test_health.py`
- `Makefile` (targets: `dev`, `test`, `lint`, `migrate`, `up`, `down`)
- `.github/workflows/ci.yml` (ruff + mypy + pytest)

### To modify
- (none — greenfield)

## Implementation Steps

1. **Init repo**: `git init`; add `.gitignore` (Python, Node, `.env`, `*.pem`, `~/.codex`-style paths).
2. **`pyproject.toml`** with `[project]` deps pinned:
   - `fastapi==0.115.*`, `uvicorn[standard]==0.32.*`, `pydantic==2.9.*`, `pydantic-settings==2.6.*`
   - `sqlalchemy==2.0.*`, `asyncpg==0.30.*`, `alembic==1.13.*`
   - `redis==5.2.*`, `arq==0.26.*`
   - `structlog==24.4.*`, `prometheus-client==0.21.*`
   - `opentelemetry-api==1.27.*`, `opentelemetry-sdk==1.27.*`, `opentelemetry-instrumentation-fastapi==0.48b0`, `opentelemetry-exporter-otlp==1.27.*`
   - `argon2-cffi==23.1.*`, `httpx==0.27.*`
   - dev: `pytest==8.3.*`, `pytest-asyncio==0.24.*`, `ruff==0.7.*`, `mypy==1.13.*`
3. **`src/settings.py`** — `class Settings(BaseSettings)`. Required vars listed in table below. `Settings()` is module-level singleton via `@lru_cache`.

   | Env var | Type | Default | Notes |
   |---|---|---|---|
   | `WRAPPER_ENV` | str | `dev` | dev/staging/prod |
   | `DATABASE_URL` | str | — | `postgresql+asyncpg://...` |
   | `REDIS_URL` | str | — | `redis://redis:6379/0` |
   | `CODEX_BIN` | str | `codex` | Path to codex CLI |
   | `CODEX_AUTH_DIR` | str | `/codex-auth` | RO mount of `~/.codex` |
   | `WORKSPACE_ROOT` | str | `/workspaces` | Per-job ephemeral dirs |
   | `LOG_LEVEL` | str | `INFO` | |
   | `OTEL_EXPORTER_OTLP_ENDPOINT` | str? | `None` | If unset → no-op tracer |
   | `OTEL_SERVICE_NAME` | str | `codex-wrapper-gateway` | |
   | `JOB_TIMEOUT_SECONDS` | int | `900` | Default 15 min |
   | `JOB_CANCEL_GRACE_SECONDS` | int | `5` | SIGTERM → SIGKILL |

4. **`src/observability/logging.py`** — structlog processors chain:
   - `add_log_level`, `add_logger_name`, `TimeStamper(fmt='iso')`
   - **Custom `RedactProcessor`**: scrub keys matching regex `(?i)(authorization|api[-_]?key|codex[-_]?api[-_]?key|openai[-_]?api[-_]?key|secret|token|password)` → replace value with `***REDACTED***`. Recursive into nested dicts.
   - `JSONRenderer`
   - Bind `service`, `env` from settings.
5. **`src/observability/tracing.py`** — if `OTEL_EXPORTER_OTLP_ENDPOINT` set: install OTLP exporter + `FastAPIInstrumentor`. Else: no-op `TracerProvider`.
6. **`src/observability/metrics.py`** — create global `CollectorRegistry`. Expose `/metrics` route via `prometheus_client.make_asgi_app`. (Counters/histograms added in phase 7.)
7. **`src/db/engine.py`** — TWO pools (per C8/C9):
   - **Main async engine** (request path): `create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=10, pool_timeout=2.0)`. Code comment documents math: 100 RPS × ~50ms p99 = ~5-15 simultaneous; pool_timeout=2.0 avoids 30s hang under burst (caller sees fast 503 instead).
   - **Background writes engine** (audit / last_used_at fire-and-forget): `create_async_engine(settings.DATABASE_URL, pool_pre_ping=True, pool_size=3, max_overflow=0, pool_timeout=0.5)`. On acquire timeout: caller MUST log WARN + drop the write — never block request path.
   - Two `async_sessionmaker(...)` factories: `async_session()` (default, bound to main engine) and `bg_session()` (bound to background-writes engine). `get_session()` dep returns main only. Phase 01 consumes `bg_session()` for `update_last_used_fire_and_forget`.
   - Postgres `max_connections` reminder: at 4 uvicorn workers × (20+10) main + (3) bg = 132 conns/gateway; phase 10 must bump `max_connections` (default 100) or add pgBouncer (MM11).
8. **`src/db/models.py`** — `class Base(DeclarativeBase)` only. Tables added in later phases; alembic autogenerate will pick them up.
9. **`src/redis_client.py`** — `redis.asyncio.from_url(...)` returning a singleton pool. Provide `get_redis()` dep + `close()` for lifespan shutdown.
10. **`src/gateway/health.py`** — two routes:
    - `/healthz`: always `{"status":"ok"}`.
    - `/readyz`: ping DB (`SELECT 1`) + Redis (`PING`); 503 if either fails.
11. **`src/gateway/app.py`** — `def create_app() -> FastAPI`:
    ```python
    app = FastAPI(title="codex-wrapper", version=...)
    app.router.lifespan_context = lifespan  # init logging/otel/db/redis
    app.include_router(health_router)
    app.mount("/metrics", make_asgi_app())
    return app
    ```
    Lifespan: init order from §Architecture above; on shutdown close engine + redis.
12. **Alembic init**: `alembic init src/db/migrations`. Edit `env.py` to import `Base.metadata` and use `settings.DATABASE_URL` (sync URL for migrations: strip `+asyncpg`). Generate empty initial revision: `alembic revision -m "init"` (autogen with empty Base — no tables yet).
13. **`Dockerfile.gateway`**:
    ```dockerfile
    FROM python:3.12-slim
    RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
        && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
        && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*
    RUN npm install -g @openai/codex@0.125.0
    WORKDIR /app
    COPY pyproject.toml uv.lock ./
    RUN pip install uv && uv sync --frozen --no-dev
    COPY src ./src
    COPY alembic.ini ./
    EXPOSE 8000
    CMD ["uv","run","uvicorn","src.gateway.app:create_app","--factory","--host","0.0.0.0","--port","8000"]
    ```
14. **`Dockerfile.worker`** — same base + `apt-get install -y git`. CMD = `uv run arq src.workers.arq_worker.WorkerSettings` (worker module created phase 5; placeholder allowed now).
15. **`docker-compose.yml`** — services: `gateway`, `worker`, `postgres:16-alpine`, `redis:7-alpine`, `caddy:2`, `otel-collector:0.110.0`. Mount `~/.codex:/codex-auth:ro` on gateway+worker. Healthchecks for postgres/redis. Gateway depends_on Postgres healthy.
16. **`Caddyfile`** dev: `localhost { reverse_proxy gateway:8000 }`. Prod profile defers to phase 10.
17. **Tests**:
    - `tests/unit/test_settings.py`: `Settings()` raises on missing required.
    - `tests/unit/test_health.py`: `httpx.AsyncClient` against test app, assert 200 / 503 paths (mock DB ping).
18. **CI** (`.github/workflows/ci.yml`): matrix `python: [3.12]`. Steps: `uv sync` → `ruff check` → `ruff format --check` → `mypy src` → `pytest -q`.
19. **Makefile**: `dev` (uvicorn reload), `up` (`docker compose up --build`), `migrate` (`alembic upgrade head`), `lint`, `test`, **`verify-codex`** (runs `scripts/verify-codex.sh` inside gateway container; non-zero exit fails CI/bootstrap).
20. **`.env.example`** — every settings key with comment + safe placeholder. Includes `DB_POOL_SIZE=20`, `DB_MAX_OVERFLOW=10`, `DB_POOL_TIMEOUT=2.0`, `BG_DB_POOL_SIZE=3`, `BG_DB_POOL_TIMEOUT=0.5`.
21. **`scripts/verify-codex.sh`** (Codex pre-flight, addresses C1 + C11):
    ```bash
    #!/usr/bin/env bash
    set -euo pipefail
    # 1) version pin
    V=$(codex --version | awk '{print $NF}')
    [[ "$V" == "0.125.0" ]] || { echo "version mismatch: $V != 0.125.0"; exit 2; }
    # 2) required flags present in help text
    HELP=$(codex exec --help 2>&1)
    for f in ephemeral skip-git-repo-check json sandbox cd ask-for-approval color; do
      echo "$HELP" | grep -qE -- "--$f" || { echo "missing flag: --$f"; exit 3; }
    done
    # 3) JSONL on stdout still works
    OUT=$(echo "" | codex exec --json --color never --skip-git-repo-check --sandbox read-only \
      --ask-for-approval never "say pong" 2>/dev/null | head -n 1)
    [[ "$OUT" =~ ^\{\"type\":\"thread\.started\" ]] || \
      [[ "$OUT" =~ ^\{ ]] || { echo "stdout not JSONL: $OUT"; exit 4; }
    # 4) flag changelog probe — researcher-01 §6 mentions Unix socket transport in 0.125.0
    if echo "$HELP" | grep -qiE "unix.?socket|--io"; then
      echo "WARN: 0.125.0 advertises unix-socket transport — phase-02 must validate stdout pipe still default"
    fi
    echo "verify-codex OK"
    ```
    `make verify-codex` must run AFTER `make up` and BEFORE phase-02 implementation begins. Document in phase-02 dependency list.
22. **`src/gateway/sse_helpers.py`** (addresses MM1, used by phases 03/04/05):
    ```python
    async def keepalive_wrap(upstream: AsyncIterator[bytes], interval: float = 15.0) -> AsyncIterator[bytes]:
        """Yield upstream bytes; emit `: keepalive\n\n` SSE comment when idle > interval seconds."""
        agen = upstream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(agen.__anext__(), timeout=interval)
                yield chunk
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
            except StopAsyncIteration:
                return
    ```
    Cadence: 15s (under typical Caddy/CDN/AWS-ALB 30-60s idle defaults). Docs note that this only emits during silence — not on top of normal traffic.

## Todo List
- [ ] `pyproject.toml` + `uv.lock` committed
- [ ] `src/settings.py` with Settings class
- [ ] structlog config with redaction processor
- [ ] OTEL no-op + active modes
- [ ] Prometheus `/metrics` mount
- [ ] DB engine + session factory
- [ ] Redis pool
- [ ] `/healthz` + `/readyz` routes
- [ ] FastAPI factory + lifespan wiring
- [ ] Alembic init + empty revision
- [ ] Dockerfile.gateway with codex 0.125.0 pinned
- [ ] Dockerfile.worker
- [ ] docker-compose.yml with all services
- [ ] Caddyfile dev
- [ ] Unit tests pass
- [ ] CI workflow green
- [ ] Makefile + .env.example
- [ ] `scripts/verify-codex.sh` and `make verify-codex` succeeds against pinned 0.125.0
- [ ] `src/gateway/sse_helpers.py` keepalive util + unit test (idle stream emits `: keepalive\n\n` every 15s)
- [ ] Two-pool DB engine wired (main + background); both ping in lifespan

## Success Criteria
- `docker compose up` exits 0 to ready state in < 60s on cold cache.
- `curl http://localhost/healthz` → `{"status":"ok"}`.
- `curl http://localhost/readyz` → 200 when DB+Redis up; 503 when one stopped.
- `docker compose exec gateway codex --version` prints `0.125.0`.
- `make verify-codex` exits 0 (asserts version + required flags + JSONL-stdout still works); CI gate.
- `alembic upgrade head` runs cleanly against fresh Postgres.
- Two DB pools (main + background) both healthy in `/readyz`; pool sizes match `.env` config.
- `keepalive_wrap` unit test: feeds slow async-iterator (yield only every 30s), asserts `: keepalive\n\n` emitted at ~15s.
- `pytest tests/unit -q` ≥ 5 tests pass.
- ruff + mypy clean.
- No file in `src/` exceeds 200 LOC.

## Risk Assessment
| Risk | Mitigation |
|---|---|
| Codex CLI version drifts during build | `npm i -g @openai/codex@0.125.0` exact pin in Dockerfile; `make verify-codex` (CI + bootstrap gate) asserts version + required flags + JSONL-stdout default |
| 0.125.0 introduces unix-socket transport, breaks stdout pipe assumption (researcher-01 §6) | `verify-codex.sh` step 4 greps help for `unix.?socket\|--io`; warns + creates phase-02 follow-up TODO if found |
| DB pool exhaustion under burst | Two-pool design: main (20+10) for requests, separate (3) for background writes with 0.5s timeout — drops on contention; `pool_timeout=2.0` on main means fast 503 (not 30s hang) |
| `~/.codex` mount missing → all phases break | Lifespan readiness check fails fast if `CODEX_AUTH_DIR/auth.json` not present (gate behind `WRAPPER_ENV=prod`) |
| pydantic-settings + alembic env conflict | Use sync URL derivation helper in `env.py`; document in code comment |
| Image bloat from node + python | `--no-install-recommends`, slim base; CI gate at 800 MB |
| structlog redaction false negatives | Unit test feeds known secret keys + asserts `***REDACTED***` |

## Security Considerations
- `~/.codex` mounted **read-only**; container cannot mutate session.
- No `.env` committed; CI uses GH secrets.
- Caddy auto-ACME deferred to phase 10 prod profile.
- All log output piped to stdout JSON only (no file writes from app code).

## Next Steps
- Phase 1 plugs into `Base` + adds `users`/`api_keys` tables and bearer auth middleware.
- Phase 2 consumes `CODEX_BIN` + `CODEX_AUTH_DIR` settings to spawn subprocess.
