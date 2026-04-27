# Phase 00 Code Review

## Verdict
**APPROVE_WITH_CHANGES**

Phase 00 is mostly well-built — clean module boundaries (all 12 src files <125 LOC, well under the 200-LOC cap), tests pass (33/33), ruff/mypy strict are green, and the red-team-driven design choices (two-pool DB, keepalive_wrap, redaction processor, codex pre-flight) are all present and structurally correct. However there are **three critical bugs that will manifest the moment Phase 01 deploys**: (1) `/readyz` will always 503 because `health.py` imports stale module-level globals by-reference; (2) `alembic upgrade head` will crash with `ModuleNotFoundError: No module named 'psycopg2'` because the sync driver was never declared as a dependency; (3) `verify-codex.sh` cannot run because `scripts/` is never copied into the gateway image. Two of those break stated success criteria. Fix all three before phase 01 starts.

---

## Critical Issues (block phase 01)

### C-1. `/readyz` always reports DB and Redis "not initialised" — stale `from … import` bindings
**File:** `src/gateway/health.py:20-21`
```python
from src.db.engine import _main_engine
from src.redis_client import _client as _redis_client
```
These bind a *local* name to whatever the global is at import time (always `None`, because `init_engines()` has not run yet). When `init_engines()` later does `global _main_engine; _main_engine = create_async_engine(...)`, it rebinds the name in `src.db.engine`'s namespace — **not** the alias inside `src.gateway.health`.

Verified empirically:
```
initial via direct import: None
initial via module attr:   None
after init via direct import: None     ← the readyz handler sees this
after init via module attr:   <AsyncEngine object>
```

**Impact:** in production `/readyz` returns `503 {"errors":["db: engine not initialised","redis: client not initialised"]}` forever — Caddy/k8s health checks will refuse to route traffic to the gateway. The unit tests pass only because they `patch("src.gateway.health._main_engine", mock_engine)` which writes to the *local stale alias*; they do not exercise the real init path.

**Fix:** import the modules and read attributes lazily:
```python
from src.db import engine as _db
from src import redis_client as _rc
...
if _db._main_engine is None: ...
async with _db._main_engine.connect() as conn: ...
if _rc._client is None: ...
await _rc._client.ping()
```
Or (cleaner) expose accessor functions `get_main_engine() -> AsyncEngine | None` and `get_redis_client() -> Redis | None` and stop touching private names from another module entirely. The latter also fixes the leading-underscore-as-public-API smell flagged below in M-1.

### C-2. `alembic upgrade head` crashes — no sync Postgres driver declared
**File:** `pyproject.toml:6-24`, `src/db/migrations/env.py:31-37`
`env.py` strips `+asyncpg` to produce `postgresql://...`, then `engine_from_config(...)` resolves to the default `psycopg2` dialect driver. `psycopg2` (and `psycopg`/`psycopg3`) are not listed in `[project].dependencies`. Verified:
```
$ uv run python -c "import psycopg2"
ModuleNotFoundError: No module named 'psycopg2'
```
**Impact:** Phase 00 success criterion "`alembic upgrade head` runs cleanly against fresh Postgres" fails. Every phase 01+ migration breaks at deploy.

**Fix options (pick one):**
- Add `"psycopg[binary]==3.2.*"` to deps and rewrite `_sync_url` to produce `postgresql+psycopg://...` (recommended — psycopg3 is the supported successor, asyncpg already in deps).
- Add `"psycopg2-binary==2.9.*"` to deps (works with the current `re.sub(r"\+asyncpg", "", ...)` — minimal change).
- Run alembic *async* (use `connectable.connect()` via `create_async_engine` and `connection.run_sync(do_migrations)`); avoids needing a second driver entirely.

The doc comment in `env.py:36` ("psycopg2 is installed as a fallback for migrations only") is currently a lie.

### C-3. `make verify-codex` cannot find the script — `scripts/` not copied into image
**File:** `Dockerfile.gateway:21-22`, `Makefile:55`
The Dockerfile copies only `src` and `alembic.ini`:
```dockerfile
COPY src ./src
COPY alembic.ini ./
```
Then `Makefile:55` runs `docker compose exec gateway bash /app/scripts/verify-codex.sh`. That path does not exist in the image. Result: `bash: /app/scripts/verify-codex.sh: No such file or directory`.

**Impact:** Phase 00 success criterion "`make verify-codex` exits 0" cannot pass. Spec §Implementation step 19 (`make verify-codex` runs `scripts/verify-codex.sh` inside gateway container) is broken. Phase 02 has this gating its start.

**Fix:** add `COPY scripts ./scripts` to both Dockerfiles (and `RUN chmod +x scripts/verify-codex.sh` if the host filesystem doesn't preserve the executable bit — currently the Makefile invokes via `bash /app/...` so the bit isn't strictly required, but recommend it anyway).

---

## High Issues

### H-1. Lifespan does not actually verify DB / Redis connectivity at startup
**File:** `src/gateway/app.py:53-63`, spec §Architecture step 4-5
The spec says "Open SQLAlchemy engine **+ connection check**" and "Open Redis pool **+ ping**". The docstring at `app.py:12-13` mirrors this. But the actual implementation only calls `init_engines()` and `init_redis()` — both create lazy pools and return immediately. No `SELECT 1`, no `await client.ping()`. Misconfigured `DATABASE_URL` will fail open: the gateway will start, accept traffic, and only crash on the first real query.

**Impact:** No fast-fail on bad config. Combined with C-1, the `/readyz` 503 might be the only signal — but C-1 makes that signal permanent and noisy. Even after C-1 is fixed, you want startup-time validation.

**Fix:** at the end of `init_engines()` or in lifespan after the call, do:
```python
async with _main_engine.connect() as conn:
    await conn.execute(text("SELECT 1"))
async with _bg_engine.connect() as conn:
    await conn.execute(text("SELECT 1"))
```
And after `init_redis()`:
```python
await _client.ping()
```
Let exceptions propagate — uvicorn will exit with non-zero, which is what you want.

### H-2. `~` not expanded in `CODEX_AUTH_HOST_DIR=~/.codex`
**File:** `.env.example:32`
```
CODEX_AUTH_HOST_DIR=~/.codex
```
Docker Compose does **not** expand `~`. With `.env` overriding the default `${HOME}/.codex` fallback in `docker-compose.yml:44`, the bind-mount source becomes the literal string `~/.codex` resolved relative to compose's CWD — which silently creates a `~` directory at the compose project root the first time `docker compose up` runs (compose creates non-existent host paths as empty dirs by default). Auth is then completely missing inside the container.

**Impact:** Codex CLI inside the container will see an empty `/codex-auth`; every Codex call fails auth in phase 02. Symptom is hard to diagnose because the gateway boots fine.

**Fix:** either drop the `CODEX_AUTH_HOST_DIR` line from `.env.example` (so the compose default `${HOME}/.codex` always wins) **or** rewrite the example to `CODEX_AUTH_HOST_DIR=${HOME}/.codex` (which compose *does* expand) **and** document that `~` literal does not work.

### H-3. Pool-sizing math doesn't match runtime worker count
**File:** `Dockerfile.gateway:29`, `src/db/engine.py:17`, `.env.example:13`
Engine docstring says "At 4 uvicorn workers × (20+10) main + (3) bg = 132 conns/gateway" but the Dockerfile launches with `--workers 1`. Either (a) the comment is misleading and should say "1 worker, scale-out via container replicas", or (b) the Dockerfile should use `--workers 4` (less common in container deployments today). Picking 1 is fine, but the 132-conn warning is then false alarm and the (20+10) main pool is over-provisioned for a single async event loop.

**Impact:** future-eng confusion; pgBouncer / `max_connections` planning in phase 10 is based on wrong baseline.

**Fix:** reconcile the comment in `engine.py` lines 7-19 with `Dockerfile.gateway:29`. State the deployment model explicitly. If sticking with 1 worker, drop pool size (e.g., 10+5) or document that overhead is intentional headroom for concurrent SSE connections (each holds a session for streaming duration if not careful — phase 03/04 should NOT hold a session for SSE lifetime; flag this for that review).

---

## Medium Issues

### M-1. Cross-module access to private names (leading underscore)
**File:** `src/gateway/health.py:20-21`, `tests/unit/test_health.py:28-30,52-53,79-80,103-104`
`_main_engine`, `_client`, `_pool` are leading-underscore names → conventionally private. They're accessed (and patched) from outside their defining module. Even after fixing C-1, prefer accessor functions (`get_main_engine()` etc.) so the contract is explicit. This also makes the tests less coupled to internal naming.

**Fix:** add public accessors:
```python
# in src/db/engine.py
def get_main_engine() -> AsyncEngine | None:
    return _main_engine

# in src/redis_client.py
def get_client() -> Redis[Any] | None:
    return _client
```
Then `health.py` and tests use those. Solves C-1 *and* M-1 together.

### M-2. `RedactProcessor` doesn't traverse tuples; mutates dict during iteration only safely-by-luck
**File:** `src/observability/logging.py:42-69`
- `_redact_value` handles `dict` and `list` but **not** `tuple`. Pydantic models that serialize to tuples, or upstream HTTP libs (e.g. httpx response history) that return tuples, will leak. Easy to add: `if isinstance(value, tuple): return tuple(_redact_value(v) for v in value)`.
- The `__call__` in `RedactProcessor` does `for key in list(event_dict.keys())` (good — snapshot), then `event_dict[key] = _redact_value(...)` for non-secret keys. This re-runs `_redact_value` on the entire value tree even when the key is innocuous. For large nested payloads this is O(N) extra walk per log event with no caching. Not a hot path *yet*, but if phase 04 logs full Responses payloads on debug, watch it.
- Pydantic `BaseModel` and dataclass values are also untouched — they have keys called `api_key` but `_redact_value` returns them unchanged because `isinstance(value, dict)` is False. Phase 03+ may log Pydantic models; consider `model_dump()`-then-redact in callers, or extend processor.

**Impact:** real-world secret-leak gaps. Tests pass because they only feed dicts/lists.

**Fix:** add tuple branch; document in module docstring that callers logging Pydantic models must `.model_dump()` first; add tests for tuple + Pydantic.

### M-3. `bg_session()` exception type is wrong in docstring
**File:** `src/db/engine.py:93`
Docstring claims "On pool timeout a `TimeoutError` is raised". SQLAlchemy actually raises `sqlalchemy.exc.TimeoutError` (a subclass of `sqlalchemy.exc.OperationalError`), **not** the builtin `TimeoutError`. Phase 01 readers will write `except TimeoutError` and miss it.

**Fix:** correct the docstring to `sqlalchemy.exc.TimeoutError`, or in phase-01 import alias for clarity. (Could also wrap-and-reraise as `asyncio.TimeoutError` here, but that's surgery for later.)

### M-4. `health.py` catches bare `Exception` for connectivity probes — fine for readyz, but logs full exception string back to client
**File:** `src/gateway/health.py:50-52, 60-62, 65`
```python
errors.append(f"db: {exc}")
...
return JSONResponse({"status": "unavailable", "errors": errors}, status_code=503)
```
The 503 body returns the full exception message to the *external HTTP caller*. `asyncpg`'s exception strings include hostnames, usernames, and sometimes connection-string fragments. That's a small data leak via readyz — anyone who can hit the endpoint sees DSN structure.

**Impact:** Caddy/k8s probes are localhost, but if `/readyz` is exposed externally (it's mounted at root, so yes), this leaks infra detail.

**Fix:** log the full exception (already doing that with `logger.warning`), but return a generic message to the client:
```python
errors.append("db: unreachable")
errors.append("redis: unreachable")
```

### M-5. CI does not actually run `verify-codex` (only syntax-checks the script)
**File:** `.github/workflows/ci.yml:50-51`
```yaml
- name: Verify shell script syntax
  run: bash -n scripts/verify-codex.sh
```
That's `bash -n` — parse only, never executes. Spec step 19 says "non-zero exit fails CI/bootstrap". Currently CI cannot detect a real Codex version drift — only a typo in the script.

**Justification accepted:** Codex auth (chatgpt session) is not available on GH runners, so we can't run the full check there. But the spec's CI gate is unsatisfied. Note this as a known gap; either install Codex CLI in CI without auth and run *only* `--version` + flag-presence checks (steps 1+2), or accept and document that `make verify-codex` is a developer-machine gate, not a CI gate. Update the spec to match.

### M-6. `make migrate` runs alembic against host DATABASE_URL, not container — combined with C-2 it'll fail twice
**File:** `Makefile:34`
`uv run alembic upgrade head` runs in the host venv. The host venv has no Postgres driver (C-2). And `DATABASE_URL` in `.env` points to `localhost:5432` — only works if Postgres is exposed on host (it is, line 17 of compose). Documenting only.

---

## Low / Nitpicks

### L-1. `tests/conftest.py:25-26` mutates global `os.environ` at import time
Setting env vars via `os.environ.setdefault(...)` at module top means every test process inherits them. That's *intentional* (so Settings() doesn't fail on import elsewhere), but the side-effect-on-import pattern is fragile — a future test importing `Settings` *before* conftest loads (e.g., a plugin) would still fail. Consider `monkeypatch.setenv` in a session fixture, or guard with `if "DATABASE_URL" not in os.environ`.

### L-2. `Settings` class uses `model_config = SettingsConfigDict(env_file=".env", ...)` — but in containers there is no `.env` file (only env vars from `env_file: .env` in compose, which exports them as real env vars). Harmless because pydantic-settings tolerates a missing file silently, but worth a comment.

### L-3. `src/observability/metrics.py:14` uses `from typing import Any` then `# noqa: ANN401`. Could just declare return as `ASGIApp` (from `starlette.types`) — drops the noqa and gives mypy real signal.

### L-4. `src/redis_client.py:54` does `getattr(_client, "aclose", None) or _client.close` to handle redis-py version drift. Pinned to `redis==5.2.*` which has `aclose`; the fallback is dead code and mildly confusing. Drop it.

### L-5. `src/gateway/app.py:88` — `docs_url="/docs" if settings.wrapper_env != "prod" else None` — agrees with spec spirit but disables `/openapi.json` schema fetching for SDK codegen in prod. Keep `/openapi.json` accessible if you want SDK consumers to introspect; only `/docs` (Swagger UI) is the leak surface.

### L-6. `tests/unit/test_sse_helpers.py:67-69, 110-113` — mocking `asyncio.wait_for` and calling `coro.close()` to swallow the un-awaited coroutine works but is fragile. Future Python versions may emit a `RuntimeWarning: coroutine ... was never awaited`. The test also doesn't validate that `wait_for` was called *with* the right coroutine. Could just use a real slow async iterator with `interval=0.05` (proven working — see my live test). Trade off speed (≤ 200ms) for robustness.

### L-7. `src/db/migrations/env.py:36` — comment claims "psycopg2 is installed as a fallback for migrations only" — false (see C-2). Either install it or rewrite the comment.

### L-8. `pyproject.toml` has both `[project.optional-dependencies] dev = [...]` (line 26-33) and `[dependency-groups] dev = [...]` (line 62-69) — duplicate. uv accepts both but it's confusing and easy to drift. Pick one (uv prefers `[dependency-groups]` for uv-native, `[project.optional-dependencies]` for PEP 735 broad compat). Drop the other.

### L-9. `Dockerfile.gateway:19` runs `pip install --no-cache-dir uv && uv sync --frozen --no-dev`. Could use the upstream `ghcr.io/astral-sh/uv` slim image as a build stage and copy the `uv` binary in — saves ~50 MB.

### L-10. `otel-collector-config.yaml` is referenced by compose at line 89 but lives at repo root, not under a config dir. Fine for now, but as more config files appear it'll get cluttered. Consider `config/otel-collector.yaml`.

### L-11. `tests/unit/test_settings.py:73-99` tests defaults via fresh `Settings(...)` calls but `get_settings()` is `@lru_cache(maxsize=1)` — once any test calls it the cache poisons subsequent tests. Currently no test calls `get_settings()` so it's latent. If phase 01 starts using `get_settings()` in fixtures, add a fixture that does `get_settings.cache_clear()` per test.

### L-12. `Makefile:20` has `--reload` for `dev` target — fine — but doesn't pass `--reload-dir src` so editing tests triggers reloads. Cosmetic.

### L-13. `.env.example` line 32 `CODEX_AUTH_HOST_DIR=~/.codex` — see H-2; also there's no comment explaining that this is the **host** path, not the container path (which is `CODEX_AUTH_DIR`). Easy to confuse.

---

## Spec adherence

### What matches (good)
- Step 1 (gitignore): present, covers Python/Node/`.env`/`*.pem`.
- Step 2 (pyproject deps): all 18 listed packages pinned to spec versions.
- Step 3 (Settings): every required field + sensible defaults; `@lru_cache` singleton via `get_settings()`.
- Step 4 (logging redaction): `RedactProcessor` regex matches spec; processor chain order matches spec; service+env contextvars bound.
- Step 5 (OTEL): two paths (active OTLP vs no-op); FastAPIInstrumentor only on active path.
- Step 6 (metrics): dedicated registry; `/metrics` ASGI mount; multiprocess-mode aware.
- Step 7 (two-pool DB): both engines created with the exact spec params (20/10/2.0 main; 3/0/0.5 bg). Math comment present and matches spec wording.
- Step 8 (Base only): `DeclarativeBase`, no models.
- Step 9 (redis pool): from_url, max_connections=50, get_redis dep + close_redis.
- Step 10 (health): `/healthz` + `/readyz` with separate DB and Redis paths and 503 on either failure (modulo C-1 stale binding bug).
- Step 11 (FastAPI factory): create_app, lifespan, health router, /metrics mount, docs disabled in prod.
- Step 12 (alembic init): script_location, file_template, env.py imports Base, sync URL helper.
- Step 13/14 (Dockerfiles): codex@0.125.0 pinned, Python 3.12-slim, uv sync; worker has git extra. Modulo C-3 for verify-codex.
- Step 15 (compose): all 6 services, healthchecks, RO bind mount of `~/.codex` (good — see H-2 caveat on host-path expansion).
- Step 16 (Caddyfile): dev profile only.
- Step 17 (tests): all four required test files exist; 33 tests pass.
- Step 18 (CI): ruff + format + mypy + pytest in correct order.
- Step 19 (Makefile): all targets present including `verify-codex`. (Modulo C-3.)
- Step 20 (.env.example): every settings key documented.
- Step 21 (verify-codex.sh): all 4 checks (version, flags, JSONL, unix-socket probe) present with proper exit codes 2/3/4. Slightly improved over spec by tolerating "no auth" environment with WARN instead of fail.
- Step 22 (sse_helpers): keepalive_wrap matches spec exactly. Default 15s. Live-tested with real timing — works.

### What diverges
- **Step 17 success criterion ≥ 5 unit tests**: 33 ✓ (over-delivered).
- **Spec table for Settings step 3**: `CODEX_AUTH_DIR` default `/codex-auth` — matches. All fields match.
- **Lifespan order step 11 says "Open SQLAlchemy engine + connection check" / "Open Redis pool + ping"**: H-1 — connection check / ping not implemented. Functional divergence.
- **Step 19 verify-codex Makefile target**: implemented per spec, but C-3 prevents it from working at runtime.
- **`Dockerfile.gateway` uses `--workers 1`** vs spec's implicit 4-worker math: H-3.

---

## Strengths

1. **Two-pool DB design is correctly separated.** Each engine has its own `async_sessionmaker`; `bg_session()` returns from the bg factory; `get_session()` from the main factory. No shared state — pool exhaustion in one cannot block the other. The `max_overflow=0` hard cap on the bg pool prevents leakage. Spot on.
2. **Module sizes are tight.** Largest src file is `logging.py` at 124 LOC; everything else <100. Following the <200 rule with margin so phase 01-02 additions stay legal.
3. **`keepalive_wrap` is minimal and correct.** Live test confirmed: 0.05s interval against a 2s-delayed upstream emits keepalive promptly, doesn't double-yield, terminates cleanly on StopAsyncIteration. The `TimeoutError` (Python 3.11+) handles both `asyncio.TimeoutError` and builtin (alias since 3.11) correctly.
4. **`RedactProcessor` covers the high-value cases** (top-level + nested dicts + lists, case-insensitive, partial-match `x_authorization_y` works). Good test coverage of those paths. Gaps in M-2 are real but secondary.
5. **`extra="ignore"` on Settings** prevents brittle CI failures from unrelated env vars. Pragmatic.
6. **Tests are real, not stubs.** `test_health.py` does mock the engines but exercises the actual route handler logic, error-list assembly, status codes, and the JSON shape. `test_logging_redaction.py` covers the parameterized + nested + deep cases. `test_settings.py` actually validates that `ValidationError` fires on missing required fields by clearing env. Solid for phase-00.
7. **`.dockerignore` excludes `.env`** explicitly — secrets cannot leak into the image even if a developer drops a `.env` in the repo root.
8. **Alembic env.py works offline + online** and uses `pool.NullPool` for migrations (correct — migrations are short-lived, no need to hold pool slots).
9. **Good docstrings everywhere.** Each module's purpose, usage example, and gotchas are documented at the top. This pays back for the next dev (and the next agent).

---

## Unresolved questions

1. **Is the deployment model really 1 uvicorn worker per container with horizontal container scaling, or 4 workers per container?** This determines whether the (20+10) main pool is right or 4× over-provisioned. Need spec author confirmation; affects phase 10 capacity planning.
2. **Should `/readyz` be exposed externally (via Caddy) or only on the local docker network?** The current Caddyfile reverse-proxies everything, including `/readyz`. If externally exposed, M-4 (DSN leakage in error strings) matters; if internal-only, it's cosmetic.
3. **Is psycopg2 vs psycopg3 a settled choice?** C-2 fix could go either way. psycopg3 is the modern path and pairs better with SQLAlchemy 2.0 + asyncpg ecosystem, but adds a new dep. psycopg2-binary is smaller change. Spec didn't pick.
4. **Does CI need to actually verify codex CLI version?** M-5 — currently it doesn't. If yes, GH runners need at least `npm i -g @openai/codex@0.125.0` in a step before checking `--version`. Auth-dependent steps (3+4 of the script) would still need to run developer-side only.

---

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical issue count:** 3 (C-1 stale bindings, C-2 missing sync DB driver, C-3 scripts not in image)
