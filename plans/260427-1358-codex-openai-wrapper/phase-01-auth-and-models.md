# Phase 01: Auth & Models

## Context Links
- Brainstorm: `../reports/brainstorm-260427-1358-codex-openai-wrapper.md` (§2 wrapper auth, §5 schema, §7 risks "secret leak / API key in DB")
- Phase 00: `phase-00-bootstrap.md` (provides `Base`, async engine, settings, structlog redaction)
- OpenAI taxonomy: `research/researcher-02-openai-event-taxonomy.md` (error envelope shape — see §A.4 / §B.3.13)
- Project rules: `../../.claude/rules/development-rules.md`

## Overview
- Priority: critical
- Status: pending
- Effort: S
- Description: First feature phase. Adds the two persistent identities the platform needs: `users` and `api_keys`. Implements bearer-token middleware that authenticates every request via argon2id hash compare, an out-of-band admin endpoint for key issuance (NOT public, gated by a separate admin token), and the trivial `GET /v1/models` endpoint that the OpenAI SDK pings on startup. After this phase the gateway can identify a tenant on every request and reject unauthenticated traffic with an OpenAI-shaped error body.

## Red Team Resolutions
- **C8** — `update_last_used_fire_and_forget` redesigned: (1) tasks tracked in module-level `_BG_TASKS: set[asyncio.Task]` to prevent GC mid-execution; (2) uses dedicated `bg_session()` factory from phase-00's separate small pool (size 3, `pool_timeout=0.5`); (3) on pool-acquire timeout: log WARN + DROP — never block request path. Eliminates pool exhaustion + task-leak failure modes.
- **C9 (consumer)** — Auth lookups use main pool from phase-00 (pool_size=20, pool_timeout=2.0). Background writes are isolated.

## Key Insights
- Argon2id (via `argon2-cffi`) is required, not bcrypt — already pinned in phase 00 deps. Default params (m=64MiB, t=3, p=4) are fine for ≤ 200 keys; we hash on key creation only and on auth lookup we hash-compare, so per-request cost is one argon2 verify (~20-50 ms). For high RPS we cache `(token_prefix → user_id)` in Redis with short TTL — KISS: defer cache to phase 6 unless benchmark shows pain.
- Plaintext key shown ONCE on creation. Format: `cwk_` (codex-wrapper-key) + 32 random bytes URL-safe-b64 = 47 chars total. Prefix lets us route quick lookups (researcher-02 inspired by OpenAI's `sk-proj-...` style).
- The `last_used_at` column is a write-on-every-request hot field. Updating it inline blocks the request. Use fire-and-forget `asyncio.create_task(...)` so the response returns immediately and an UPDATE runs in the background. Stale by < 1s is acceptable. **Critical (C8)**: tasks MUST be tracked in a module-level set to prevent GC, AND must use the SEPARATE background-write pool (phase-00 `bg_session`) with a 0.5s timeout that DROPS on contention. Never share the main request pool — under burst, audit/last_used_at writes pile up and cause cascading latency.
- Admin endpoint (`POST /admin/api-keys`) is the ONLY way to create keys in v1. Protected by `X-Admin-Token` header compared against `settings.ADMIN_TOKEN` (env var). No self-service signup in v1 — locked decision (brainstorm §2).
- Error body MUST mirror OpenAI exactly so SDK error objects parse cleanly: `{"error": {"message", "type", "param", "code"}}`. Per researcher-02 §B.3.13 / §A.4. Wrong shape breaks `openai.AuthenticationError`.
- `/v1/models` returns one row, `id="codex-cli"`. Hardcoded — no DB table. We do NOT enumerate the upstream Codex's available models because Codex itself is opaque to us; we expose a single logical model (brainstorm §6 "owned_by: codex-wrapper").

## Requirements

### Functional
- Alembic migration `001_users_apikeys.py` creates `users` + `api_keys` tables (per brainstorm §5 schema sketch).
- `Authorization: Bearer <key>` middleware authenticates EVERY request to `/v1/*`. Routes that don't need auth (`/healthz`, `/readyz`, `/metrics`, `/admin/*`) are explicitly excluded.
- 401 with OpenAI-shaped error on missing/malformed/unknown/revoked key.
- `last_used_at` updated on successful auth (background task — no request blocking).
- `POST /admin/api-keys` body `{"user_email": str, "name": str, "tier": "free"|"pro"|"ent"}` → response `{"id": uuid, "key": "cwk_...", "prefix": "cwk_xxxxxxxx", "created_at": ts}`. Plaintext `key` returned ONCE; never retrievable again.
- `DELETE /admin/api-keys/{id}` sets `revoked_at = now()`; subsequent requests with that key return 401 immediately.
- `GET /v1/models` (auth-required) returns `{"object":"list","data":[{"id":"codex-cli","object":"model","created":<unix_ts_of_phase_release>,"owned_by":"codex-wrapper"}]}`.

### Non-Functional
- Argon2id parameters fixed in `src/auth/hashing.py` constants — same params for issue + verify; document if we ever change them (rotation = invalidate all keys).
- All SQL via SQLAlchemy 2.0 async ORM + typed `Mapped[...]` columns. No raw SQL except in alembic ops.
- Each Python file ≤ 200 LOC.
- 401 path latency p95 < 100 ms (cold argon2 verify against 1 known-bad hash, to prevent timing oracle).
- Constant-time hash compare — argon2-cffi `PasswordHasher.verify` is constant-time by design.

## Architecture

```
HTTP request
   │
   ▼
┌──────────────────────────────────────────────────┐
│ FastAPI app                                      │
│                                                  │
│  middleware order (top → bottom):                │
│   1. tracing (phase 7, no-op now)                │
│   2. structlog request_id binder                 │
│   3. AuthMiddleware  ◄── this phase              │
│   4. (rate-limit, phase 6)                       │
│   5. routers                                     │
└──────────────────────────────────────────────────┘
                                │
                  ┌─────────────┴─────────────┐
                  ▼                           ▼
        AuthMiddleware                 Admin routes
        (excluded paths               (X-Admin-Token only)
         skip-list)                     │
            │                           ▼
            │ extract Bearer        api_keys table
            │ split prefix          (CRUD)
            │ SELECT api_keys
            │   WHERE key_hash=...
            │   AND revoked_at IS NULL
            ▼
        request.state.api_key (id, user_id, tier)
        request.state.user_id
            │
            └─► fire-and-forget: UPDATE api_keys SET last_used_at=now()
```

Data flow on 401:
```
bad/missing token → AuthMiddleware → JSONResponse(401, openai_error_body) → client
```

Data flow on key creation (admin):
```
POST /admin/api-keys (X-Admin-Token)
  → generate plaintext "cwk_" + b64(secrets.token_bytes(32))
  → hash = ph.hash(plaintext)
  → INSERT api_keys(key_hash=hash, prefix=plaintext[:12], …)
  → return plaintext ONCE
```

## Related Code Files

### To create
- `src/db/migrations/versions/001_users_apikeys.py` (alembic migration; ≤ 80 LOC)
- `src/db/models.py` — extend phase-00 stub with `User` + `ApiKey` ORM classes (≤ 150 LOC total — split if larger)
- `src/db/crud/__init__.py`
- `src/db/crud/users.py` (`get_or_create_by_email`; ≤ 80 LOC)
- `src/db/crud/api_keys.py` (`create`, `get_by_hash`, `revoke`, `update_last_used`; ≤ 150 LOC)
- `src/auth/__init__.py`
- `src/auth/hashing.py` (argon2id wrapper, key generation; ≤ 80 LOC)
- `src/auth/bearer.py` (token extraction + verify logic; ≤ 120 LOC)
- `src/auth/errors.py` (OpenAI-shaped error envelope helpers; ≤ 80 LOC)
- `src/gateway/middleware/auth.py` (FastAPI `BaseHTTPMiddleware` subclass; ≤ 150 LOC)
- `src/gateway/routes/models.py` (`GET /v1/models`; ≤ 50 LOC)
- `src/gateway/routes/admin_api_keys.py` (`POST` + `DELETE /admin/api-keys`; ≤ 150 LOC)
- `tests/unit/test_hashing.py`
- `tests/unit/test_auth_middleware.py`
- `tests/unit/test_models_endpoint.py`
- `tests/integration/test_admin_api_keys.py` (against real Postgres via testcontainers OR docker-compose test db)

### To modify
- `src/settings.py` — add `ADMIN_TOKEN: str` (required, no default; raises if unset and `WRAPPER_ENV=prod`).
- `src/gateway/app.py` — register `AuthMiddleware`; mount `models_router` + `admin_api_keys_router`; add path skip-list constant.
- `.env.example` — document `ADMIN_TOKEN`.
- `plans/.../plan.md` (Phases table — N/A in this phase, planner already did it).

### To delete
- (none)

## Implementation Steps

1. **Add settings**
   - `src/settings.py`: add `ADMIN_TOKEN: SecretStr` field. Validator: if `WRAPPER_ENV == "prod"` and unset, raise.
   - `.env.example`: `ADMIN_TOKEN=replace-me-in-prod` with comment "used by /admin/* endpoints; rotate periodically".

2. **Define ORM models** (`src/db/models.py`, extend phase-00 file)
   ```python
   class User(Base):
       __tablename__ = "users"
       id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
       email: Mapped[str] = mapped_column(String(255), unique=True)
       created_at: Mapped[datetime] = mapped_column(server_default=func.now())

   class ApiKey(Base):
       __tablename__ = "api_keys"
       id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
       user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
       key_hash: Mapped[str] = mapped_column(String(255), unique=True)
       prefix: Mapped[str] = mapped_column(String(16), index=True)  # first 12 chars of plaintext
       name: Mapped[str] = mapped_column(String(80))
       tier: Mapped[str] = mapped_column(String(8), default="free")  # free|pro|ent
       last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
       revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
       created_at: Mapped[datetime] = mapped_column(server_default=func.now())
   ```
   If file approaches 200 LOC, split into `src/db/models/user.py` and `src/db/models/api_key.py` with re-exports.

3. **Generate migration**
   - `alembic revision --autogenerate -m "users and api_keys"` (filename rename to `001_users_apikeys.py`).
   - Review generated SQL: ensure `key_hash` unique index, `prefix` non-unique index, `email` unique.
   - Local test: `alembic upgrade head` then `alembic downgrade base` round-trip clean.

4. **Hashing module** (`src/auth/hashing.py`)
   ```python
   from argon2 import PasswordHasher
   from argon2.exceptions import VerifyMismatchError
   import secrets, base64

   _PH = PasswordHasher()  # default m=64MiB, t=3, p=4

   KEY_PREFIX = "cwk_"
   KEY_BYTES = 32  # → 43 b64url chars → total 47

   def generate_plaintext_key() -> str:
       raw = secrets.token_bytes(KEY_BYTES)
       return KEY_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")

   def hash_key(plaintext: str) -> str:
       return _PH.hash(plaintext)

   def verify_key(plaintext: str, hashed: str) -> bool:
       try: _PH.verify(hashed, plaintext); return True
       except VerifyMismatchError: return False
   ```
   Constant-time by argon2 design. Unit test asserts: same plaintext different hashes (salt), wrong plaintext fails, generated keys are unique across N=100 calls.

5. **Error envelope helpers** (`src/auth/errors.py`)
   - Per researcher-02 §A.4 + §B.3.13. Provide `openai_error(status, message, type, code, param=None) -> JSONResponse`.
   - Reusable codes: `invalid_api_key` (401 missing/bad), `revoked_api_key` (401 revoked), `permission_denied` (403). Exact body shape:
     ```json
     {"error":{"message":"Incorrect API key provided.","type":"invalid_request_error","param":null,"code":"invalid_api_key"}}
     ```

6. **CRUD** (`src/db/crud/api_keys.py`)
   - `async def create(session, user_id, name, tier) -> tuple[ApiKey, str]` returns the row + plaintext key.
   - `async def get_active_by_hash_match(session, plaintext) -> ApiKey | None` — strategy: SELECT only by `prefix = plaintext[:12] AND revoked_at IS NULL`, then verify_key against each row's hash. Prefix collision is statistically near-zero (12 chars b64) but design tolerates it.
   - `async def revoke(session, key_id)`.
   - **`update_last_used_fire_and_forget(key_id)` — redesigned per C8**:
     ```python
     # module-level set — survives across calls; prevents task GC mid-flight
     _BG_TASKS: set[asyncio.Task] = set()

     async def _do_update_last_used(key_id: UUID) -> None:
         # Use SEPARATE background-write pool (phase-00 bg_session); 0.5s timeout drops on contention
         try:
             async with bg_session() as s:  # may raise TimeoutError on pool acquire
                 await s.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=func.now()))
                 await s.commit()
         except asyncio.TimeoutError:
             logger.warning("auth.last_used.pool_timeout", key_id=str(key_id))
         except Exception:
             logger.warning("auth.last_used.bg_failed", key_id=str(key_id), exc_info=True)

     def update_last_used_fire_and_forget(key_id: UUID) -> None:
         task = asyncio.create_task(_do_update_last_used(key_id))
         _BG_TASKS.add(task)
         task.add_done_callback(_BG_TASKS.discard)  # cleanup ref on completion
     ```
   - **Alternative considered (documented, NOT chosen for v1)**: single `asyncio.Queue` consumed by one background worker task — caps concurrent DB writes at 1, eliminates pool concern entirely, but adds a queue + worker module. Defer to phase 08 if observed contention; current design is KISS.
   - **Sized for**: 100 RPS bursts. With pool=3 and 0.5s timeout: under sustained 100 RPS the bg pool will time out frequently → drops. That is FINE. last_used_at is best-effort timestamp; missing updates do not affect correctness. Alarm if drop rate > 5% (phase-07 metric).

7. **Bearer token extraction** (`src/auth/bearer.py`)
   - `extract_bearer(headers) -> str | None`: parse `Authorization: Bearer <key>`. Reject if header missing, scheme not Bearer (case-insensitive), or token doesn't start with `cwk_`.
   - Returns plaintext key or None.

8. **Auth middleware** (`src/gateway/middleware/auth.py`)
   - Subclass `starlette.middleware.base.BaseHTTPMiddleware`.
   - Path skip-list constant: `{"/healthz", "/readyz", "/metrics"}` + prefix `"/admin/"` (those have their own admin-token check).
   - Flow:
     1. If path in skip-list → `await call_next(request)`.
     2. `plaintext = extract_bearer(request.headers)` → if None → 401 `invalid_api_key`.
     3. Open async session. `api_key = await get_active_by_hash_match(session, plaintext)`.
     4. If None → 401 `invalid_api_key`.
     5. Bind to `request.state.api_key = api_key`, `request.state.user_id = api_key.user_id`.
     6. `update_last_used_fire_and_forget(...)`.
     7. `await call_next(request)`.
   - On any internal exception → 500 generic OpenAI shape (don't leak).

9. **Admin endpoint** (`src/gateway/routes/admin_api_keys.py`)
   - Dependency `verify_admin_token`: compares `X-Admin-Token` header with `settings.ADMIN_TOKEN.get_secret_value()` via `secrets.compare_digest`. 403 `permission_denied` on mismatch.
   - `POST /admin/api-keys`: body schema `AdminCreateKeyRequest{user_email, name, tier}`. Tier validated against `{free, pro, ent}`. Find-or-create user by email. Create key. Return `AdminCreateKeyResponse{id, key, prefix, tier, created_at}`. Plaintext returned exactly once.
   - `DELETE /admin/api-keys/{id}`: revoke. 204 on success, 404 if not found.

10. **Models endpoint** (`src/gateway/routes/models.py`)
    - `GET /v1/models` — depends on auth (middleware already required). Static response. `created` is a fixed unix ts hardcoded as `WRAPPER_RELEASE_TS = 1714000000` (or similar — choose one and document).
    ```python
    @router.get("/v1/models")
    async def list_models():
        return {"object":"list","data":[{"id":"codex-cli","object":"model","created":WRAPPER_RELEASE_TS,"owned_by":"codex-wrapper"}]}
    ```

11. **App wiring** (`src/gateway/app.py`)
    - `app.add_middleware(AuthMiddleware)` AFTER tracing/logging, BEFORE routers.
    - `app.include_router(models_router)`, `app.include_router(admin_api_keys_router, prefix="/admin")`.

12. **Tests**
    - `tests/unit/test_hashing.py`: roundtrip, distinct salts, key prefix invariant.
    - `tests/unit/test_auth_middleware.py`: missing header → 401; bad scheme → 401; valid key in DB → 200; revoked → 401; verifies error envelope shape exactly.
    - `tests/unit/test_models_endpoint.py`: shape matches OpenAI list-models exactly.
    - `tests/integration/test_admin_api_keys.py`: create → returned plaintext authenticates → revoke → same plaintext now 401.

13. **Local verification**
    - `alembic upgrade head` clean.
    - Bring up compose: create a key via `curl -H "X-Admin-Token: ..." -d '{"user_email":"a@b.c","name":"smoke","tier":"free"}' http://localhost:8000/admin/api-keys`.
    - `curl -H "Authorization: Bearer <returned-key>" http://localhost:8000/v1/models` → 200.
    - `curl http://localhost:8000/v1/models` → 401 with shape verified.

## Todo List
- [ ] Add `ADMIN_TOKEN` to settings + `.env.example`
- [ ] Extend `src/db/models.py` with `User` + `ApiKey`
- [ ] Generate alembic migration `001_users_apikeys.py` and round-trip test
- [ ] `src/auth/hashing.py` with argon2id + plaintext generator
- [ ] `src/auth/errors.py` OpenAI-shape envelope
- [ ] `src/db/crud/users.py` get-or-create
- [ ] `src/db/crud/api_keys.py` create / lookup-by-hash / revoke / update_last_used fire-and-forget (uses `bg_session`, tracks tasks in `_BG_TASKS`, drops on pool timeout)
- [ ] `src/auth/bearer.py` header parser
- [ ] `src/gateway/middleware/auth.py` middleware with skip-list
- [ ] `src/gateway/routes/admin_api_keys.py` POST + DELETE
- [ ] `src/gateway/routes/models.py` GET
- [ ] Wire into `src/gateway/app.py`
- [ ] Unit tests (hashing, middleware, models)
- [ ] Integration test (admin flow against real Postgres)
- [ ] Manual smoke: create-key → 401-without → 200-with → revoke → 401-after

## Success Criteria
- `alembic upgrade head` then `alembic downgrade base` cleanly round-trip.
- `curl /v1/models` without auth returns 401 with body matching `{"error":{"message":...,"type":"invalid_request_error","param":null,"code":"invalid_api_key"}}` exactly.
- Admin can create a key, plaintext returned ONCE, hashed in DB (verified by SELECT — `key_hash` looks like `$argon2id$v=19$m=...`).
- After revocation the same key returns 401 within one request (no cache, immediate DB read).
- `last_used_at` updates within ≤ 1s of a successful authenticated request (verified by SELECT after request).
- All Python files ≤ 200 LOC.
- Unit + integration tests green; ruff + mypy clean.

## Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Argon2 verify cost dominates auth path under load | M | M | Acceptable for v1 (≤ 100 RPS). Cache `prefix→key_id` lookup in Redis at phase 6 if benchmark shows pain. |
| `prefix` collisions cause N candidate verifies | L | L | 12-char b64url → ~2^72 space; per-prefix cardinality stays ≈1. Loop tolerates collisions. |
| Plaintext key logged accidentally | M | HIGH | structlog redactor (phase 0) catches `Authorization`, `api[-_]?key`. Unit test feeds the literal `cwk_...` into a log call and asserts redaction. |
| Admin token leaked / committed | L | HIGH | Pydantic `SecretStr` so `repr()` is `***`; `.env.example` placeholder only; CI gitleaks scan (deferred to phase 8). |
| Race: revoke + concurrent in-flight request still completes | L | L | Acceptable. Once revoked, NEXT request fails. In-flight is bounded by single request duration. |
| Migration applied in wrong order across envs | L | M | Alembic linear history; CI gate runs `alembic upgrade head` against fresh DB on every PR. |
| `last_used_at` background task swallows DB error | M | L | Wrap in try/except, log at WARN with `key_id` (not plaintext). Best-effort field; staleness OK. **Addressed via C8 redesign**: dedicated bg pool (size 3, timeout 0.5s) drops on contention; tasks tracked in `_BG_TASKS` set to prevent GC; never blocks request path. |
| Background task GC mid-execution causes silent drops | ~~M~~ → resolved | ~~M~~ | **Addressed via C8**: `_BG_TASKS: set[asyncio.Task]` holds strong refs until `done_callback` removes them. |
| BG writes exhaust main DB pool under burst | ~~M~~ → resolved | ~~HIGH~~ | **Addressed via C8 + C9**: separate background-write pool (size 3) isolated from request pool (size 20); main pool reserved for auth lookups. |

## Security Considerations
- **Plaintext shown ONCE**: returned only from the admin POST. Stored plaintext nowhere — only hash + 12-char prefix.
- **Constant-time compare**: argon2 `verify` is constant-time. Admin token compared with `secrets.compare_digest`.
- **Skip-list**: explicit allow-list of unauth paths to prevent accidental exposure of new routes. Adding a route under `/v1/*` automatically requires auth (default-deny).
- **No self-service signup in v1** — all key issuance via admin out-of-band. Locked decision.
- **Error body uniformity**: same envelope for missing / bad / revoked → no enumeration oracle.
- **Logs**: never log `Authorization` header value, never log plaintext returned to admin (redactor enforced).
- **Backwards compat**: this phase is greenfield — no migration concerns from prior data. Future tier rename would require a data migration documented in its own phase.

## Next Steps
- Phase 02 (codex runner) is independent of auth; can develop in parallel but integration in phase 03 depends on `request.state.user_id` set here.
- Phase 06 (rate-limit) consumes `api_key.tier` to look up RPM/TPM.
- Phase 08 (hardening) adds key rotation endpoint + scheduled scrub of `last_used_at` older than 90d for audit minimization.
