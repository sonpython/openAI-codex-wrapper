# Phase 01 Code Review — Auth + /v1/models

## Verdict
**APPROVE_WITH_CHANGES**

Phase 01 is structurally well-built. Argon2id usage is correct, the C8 `_BG_TASKS` redesign is implemented exactly to spec, the C3 raw-ASGI middleware decision is honored (with a sound reason in the docstring), constant-time admin-token compare is in place, plaintext is generated/returned/never-logged, and the OpenAI error envelope shape is byte-correct. Files are well within the 200-LOC cap, mypy strict survives, and tests cover the meaningful failure modes.

That said, **two production-breaking issues** survive: (1) the auth middleware swallows broad `Exception` while inserting a `from src.auth.hashing import KEY_PREFIX` cycle into the request path that will mask real DB failures as 500s; more importantly, the middleware acquires DB sessions via `async for session in get_session(): return ...` — early-return out of an async generator that wraps `async with _main_session_factory()` is an anti-pattern that, depending on the GC/loop interleaving, **can leak connections under load** and (b) does not deterministically commit/rollback. (2) `get_active_by_prefix_and_verify` does an **N-row argon2 verify loop** under attacker-controlled prefix collisions — combined with no cap on candidates returned, an attacker who can register multiple keys (or who spams a known prefix) can amplify auth-path latency. Several other high/medium issues need attention — see below.

Counts: **Critical = 2**, High = 6, Medium = 5, Low = 4.

---

## Critical Issues (block production)

### C-1. Auth middleware leaks DB sessions via early-return from async generator
**File:** `src/gateway/middleware/auth.py:106-111`
```python
@staticmethod
async def _authenticate(request: Request, plaintext: str) -> Any:
    async for session in get_session():
        return await get_active_by_prefix_and_verify(session, plaintext)
    return None  # unreachable; satisfies mypy
```
`get_session()` (`src/db/engine.py:96-100`) is:
```python
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    assert _main_session_factory is not None, "DB not initialised"
    async with _main_session_factory() as session:
        yield session
```
The pattern `async for x in g(): return ...` causes the generator to be GC'd; CPython will eventually run `aclose()` (which raises `GeneratorExit` and triggers the `async with` `__aexit__`) — **but only when the generator is collected**, not synchronously. On asyncio event loops with bursty traffic, this means:
- The session is *not* returned to the pool synchronously after `_authenticate` returns; it sits in flight until the next GC cycle.
- If an exception bubbles up, the `__aexit__` may run on a *different* task context and cannot rollback cleanly.
- The asyncio runtime emits `RuntimeWarning: coroutine 'get_session' was never awaited` style warnings under some versions of Python.

This is masked in tests because the dependency is overridden with a yield-only `_null_gen()` that yields a `MagicMock` (no real pool semantics).

**Impact:** Under sustained 100 RPS auth load — exactly the dimension this phase was sized for — main pool (size 20, overflow 10 = 30) will be exhausted as connections sit "in use" awaiting GC. Spec §Non-Functional says "401 path latency p95 < 100 ms"; once exhausted, requests block on `pool_timeout=2.0` and you get cascading 503s.

**Fix (pick one):**
1. Drop the dependency-injection helper here; the middleware is not a FastAPI handler, so use the session factory directly:
   ```python
   from src.db.engine import _main_session_factory  # or expose accessor
   async with _main_session_factory() as session:
       return await get_active_by_prefix_and_verify(session, plaintext)
   ```
2. Or expose a typed `async_session()` context manager from `engine.py` mirroring `bg_session()` and use that here:
   ```python
   async with main_session() as session:
       return await get_active_by_prefix_and_verify(session, plaintext)
   ```
   This also fixes the `# unreachable; satisfies mypy` smell.

Also worth noting: `get_session` is currently typed as `AsyncGenerator[AsyncSession, None]` but the body has only one `yield`, so semantically it is a one-shot context manager. The `async with` pattern above is the idiomatic shape.

### C-2. Argon2 verify loop without candidate cap → attacker-controlled CPU amplification
**File:** `src/db/crud/api_keys.py:60-87`
```python
prefix = plaintext[:12]
result = await session.execute(
    select(ApiKey).where(
        ApiKey.prefix == prefix,
        ApiKey.revoked_at.is_(None),
    )
)
candidates = result.scalars().all()
for candidate in candidates:
    if verify_key(plaintext, candidate.key_hash):
        return candidate
return None
```
The phase 01 plan §Key Insights asserts "per-prefix cardinality stays ≈1" — true probabilistically for *random* prefixes from `secrets.token_urlsafe`, but **the prefix space is not attacker-controlled-resistant**. An attacker who can request creation of multiple keys (today: only via admin token, but a future tier of self-service or a leaked admin token enables this) can deliberately request keys until a chosen prefix is reached, OR can simply send `Authorization: Bearer cwk_<known prefix of valid keys>XXXXXX...` and the middleware will run argon2 verify against **every active key sharing that prefix**.

Argon2 verify is ~25-50 ms each (m=64 MiB, t=3, p=4 default). With N candidates per prefix:
- Single 200 OK request: ~25 ms.
- Single bad request hitting a prefix with N=10 collisions: ~250 ms — **all on the request path, blocking the main event loop arena allocator** (argon2 holds 64 MiB during compute).
- Sustained adversarial traffic: easily DoSable.

A second issue: the SELECT returns *all* columns of *all* matching rows. Even with cardinality 1 this is fine, but at N=100 (worst case under a deliberate prefix collision attack) it pulls 100 hashes back to the gateway just to throw them away.

**Impact:** Open auth-path DoS amplification. Spec's "p95 < 100 ms" guarantee evaporates the moment cardinality > 3.

**Fix (pick one or both):**
1. Cap candidates: add `.limit(2)` to the SELECT and reject (return None) if cardinality is >= 2 — this codifies the "prefix collisions are a configuration error, not a feature" stance and aligns with the spec's "near-zero" assumption. Bonus: 2-row LIMIT is a constant-cost SELECT.
2. Run argon2 verify in a thread pool (`asyncio.to_thread(verify_key, ...)`) so it doesn't pin the event loop. Argon2-cffi releases the GIL during the C call, so this is genuinely concurrent.

Recommended fix: do both. Cap to 1 candidate (uniqueness assumption), AND offload argon2 to a thread.

Note for future phases: the cache-by-prefix Redis layer mentioned in spec §Risk Assessment (deferred to phase 6) is the proper full mitigation, but the cap+thread fix above is the cheap right-now safety net.

---

## High Issues

### H-1. Broad `except Exception` in middleware swallows DB-pool exhaustion as 500
**File:** `src/gateway/middleware/auth.py:83-89`
```python
try:
    api_key = await self._authenticate(request, plaintext)
except Exception:
    logger.exception("auth.middleware.unexpected_error")
    response = internal_error_response()
    await response(scope, receive, send)
    return
```
Catching bare `Exception` here means `asyncio.CancelledError` is **not** caught (good, because it inherits BaseException in 3.8+), but **every other failure** — including `sqlalchemy.exc.TimeoutError` from `pool_timeout=2.0`, `OperationalError` from a transient DB blip, even programming bugs — collapses into one 500 with `{"code":"internal_error"}`. That's correct from a "don't leak details" stance, but it also means:
- Rate-limit / circuit-breaker code in phase 6 cannot distinguish DB unavailability from auth bugs.
- Operators see `auth.middleware.unexpected_error` for anything from "DB down" to "argon2 hash corrupted in storage" — high alert noise, low actionability.

**Fix:** at minimum, branch on `sqlalchemy.exc.TimeoutError` and log it as `auth.db_pool_timeout` (separate log event = separate alert rule). Optionally branch `OperationalError` (DB connectivity) → `auth.db_unavailable`. Keep the response shape uniform (still 500) so external behavior is unchanged, but make the log-event-key informative.

### H-2. `verify_key` swallows `Exception` last — masks bugs
**File:** `src/auth/hashing.py:60`
```python
except (VerifyMismatchError, InvalidHashError, Exception):
    return False
```
Listing `Exception` last after `VerifyMismatchError` and `InvalidHashError` is logically equivalent to `except Exception`. That means a `MemoryError` (argon2 needs 64 MiB), `TypeError` from bad input, `KeyboardInterrupt` (in CPython this is `BaseException`, so OK), or even `RecursionError` all return `False` silently with no log.

The docstring says "Never raises — all exceptions are absorbed and logged by callers" — but the function does NOT log. Callers receive `False` and the exception is gone.

**Impact:** A corrupted hash in DB (someone hand-edited the row) silently authenticates as 401 instead of raising an alert. A `MemoryError` — argon2's biggest real failure mode — silently auths as 401 under the same memory pressure that's killing the server.

**Fix:**
```python
from argon2.exceptions import VerifyMismatchError, InvalidHashError, InvalidHash
import structlog
logger = structlog.get_logger(__name__)
try:
    _PH.verify(key_hash, plaintext)
    return True
except VerifyMismatchError:
    return False  # expected bad-password path; no log
except (InvalidHashError, InvalidHash) as exc:
    logger.warning("auth.hash.corrupt", error=type(exc).__name__)
    return False
# Let MemoryError, TypeError, etc. propagate — they're real failures.
```

### H-3. `extract_bearer` accepts whitespace-padded scheme but rejects multi-space tokens
**File:** `src/auth/bearer.py:35-45`
```python
parts = auth_header.split(" ", 1)
if len(parts) != 2:
    return None
scheme, token = parts
if scheme.lower() != "bearer":
    return None
if not token.startswith(KEY_PREFIX):
    return None
return token
```
- Header `Authorization: Bearer  cwk_abc...` (two spaces between scheme and token) → `parts = ["Bearer", " cwk_abc..."]`, then `token` starts with a space → `token.startswith("cwk_")` → False → 401. Same for tab-separated. The RFC 7235 BNF is `BWS = *( SP / HTAB )`; some clients (curl with `-H` quoting issues, certain proxies) emit double-space.
- Conversely, `"Bearer\tcwk_abc"` (tab) → `split(" ")` doesn't split → `len(parts) == 1` → 401. Same outcome but for a different reason.
- Header value with a leading space: `" Bearer cwk_abc"` → `parts = ["", "Bearer cwk_abc"]` → scheme `""` ≠ `"bearer"` → 401.

The 401-everywhere outcome is *security-safe* (per spec §Security "uniform errors prevent enumeration") so this is a UX/compat issue not a hole. But the test for "wrong-scheme" already passes for the wrong reason in some cases.

**Impact:** Customer-reported "my key works in curl but not in <weird HTTP client>" tickets. Low impact, but the fix is one line.

**Fix:** strip+split:
```python
auth_header = auth_header.strip()
parts = auth_header.split(None, 1)  # split on any whitespace, collapse consecutive
```

### H-4. `_should_skip` uses `startswith` for `/admin/` — overlap with future `/v1/admin/...`
**File:** `src/gateway/middleware/auth.py:42-49`
```python
AUTH_SKIP_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics"})
AUTH_SKIP_PREFIXES: tuple[str, ...] = ("/admin/", "/docs", "/openapi")
```
- `/docs` is a prefix — matches `/docsfake`, `/docsanything`. Should be `/docs` exact + `/docs/...` prefix at least, or just `/docs/` (with trailing slash) — but FastAPI also serves `/docs` exact. Mild but worth tightening.
- `/openapi` likewise — matches `/openapiv2/leak-info` if anything is mounted under such a path later.
- `/admin/` is fine *for now* because no auth-required `/admin/...` exists, but if phase 8 adds (e.g.) `/admin/audit-events` that should require BOTH bearer AND admin-token, the bypass here means no bearer is checked — only the admin-token dep on the route.

The bigger latent issue: `/healthz` is *exact*. But `/healthz/sub` would not be in the set, so it'd require auth. Good. However `/metrics` is a path that gets `app.mount("/metrics", make_metrics_app())` — Starlette mounts append `/...` so all metric scrape paths under `/metrics/foo` would hit auth. Verify the mount actually serves on `/metrics` (no subpath); if it ever serves `/metrics/...` (e.g. multiple registries), add `"/metrics/"` to `AUTH_SKIP_PREFIXES`.

**Fix (defensive):**
```python
AUTH_SKIP_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics", "/docs", "/openapi.json", "/redoc"})
AUTH_SKIP_PREFIXES: tuple[str, ...] = ("/admin/", "/metrics/", "/docs/", "/openapi/")
```
And add a unit test that `/healthz/admin` does **not** bypass auth.

### H-5. Migration uses `sa.Uuid()` — not portable, and `nullable=False` defaults missing
**File:** `src/db/migrations/versions/20260427_0002_users_and_api_keys.py:35-66`
- `sa.Uuid()` (uppercase-U, lowercase-uid) is the SQLAlchemy 2.0 generic type. Under Postgres it maps to `UUID` correctly with asyncpg+psycopg. Under SQLite (used by anyone running a quick dev DB) it falls back to `CHAR(32)` — fine. But the *server-side default* for `id` is missing. Compare to `created_at` which has `server_default=sa.text("now()")`. The ORM model has `default=uuid4` (Python-side default), so SQLAlchemy generates the UUID before INSERT — but the migration table itself has no DB-level default. If anyone INSERTs raw SQL (e.g., a backup-restore script, an admin REPL), it'll fail with NOT NULL violation on `id`.
- `tier` has no `server_default` either — the ORM gives `default="free"` Python-side, same caveat.
- `users.email` is `String(255)` — fine, but no `CITEXT`-style case-insensitive constraint. Two users `Alice@x.com` and `alice@x.com` will both be admittable. Not strictly a bug given current admin-only flow, but worth noting because `EmailStr` from pydantic does **not** lowercase by default.

**Impact:** Surfaces as confusing INSERT errors during DR/restore. Email duplication latent for v2 self-service.

**Fix:**
- Add `server_default=sa.text("gen_random_uuid()")` on `id` (requires `pgcrypto` extension; or `server_default=sa.text("uuid_generate_v4()")` with `uuid-ossp`). Or accept the Python-side default as canonical and document that raw INSERT is not supported.
- Add `server_default=sa.text("'free'")` on `tier`.
- Lowercase email at the API boundary in `admin_api_keys.py:48` via a `field_validator("user_email", mode="before")` that does `.strip().lower()`. EmailStr's strict-mode does some normalization but not unconditional lowercase.

### H-6. FK `api_keys.user_id → users.id` lacks `ondelete` policy
**File:** `src/db/migrations/versions/20260427_0002_users_and_api_keys.py:63`, `src/db/models.py:61`
```python
sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
```
Default Postgres FK behavior is `NO ACTION` (= `RESTRICT`). Spec §Security says "Soft-deleted: revoked_at set to now(); row retained for audit history." — the audit-trail intent is clear. Without explicit `ondelete="RESTRICT"`, the implicit default does the right thing today, but if a future phase adds `ON DELETE CASCADE` (to wipe a user's keys when GDPR-deleting them) and forgets the audit constraint, audit history is destroyed silently.

**Impact:** Latent. Audit-trail spec ambiguity.

**Fix:** be explicit:
```python
sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
```
And in the ORM model:
```python
user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
```

---

## Medium Issues

### M-1. `revoke_api_key` does not commit on DELETE 404 path — but commits on 204 path → asymmetric session lifecycle
**File:** `src/gateway/routes/admin_api_keys.py:174-189`
```python
found = await api_keys_crud.revoke(session, key_id)
if not found:
    raise HTTPException(status_code=404, detail="api_key_not_found")
await session.commit()
```
The 404 path raises *before* the commit. SQLAlchemy's `update(...).returning(...)` will have already issued a SQL UPDATE with WHERE → 0 rows affected, no rows changed, but the (no-op) statement was sent inside an open transaction. FastAPI's `get_session` dependency in `engine.py:96-100` does NOT have a finally-block that rolls back on exception — `async with _main_session_factory()` will close the session on exception, which auto-rollbacks any open transaction. So *behavior* is fine.

But this only works because the wrapping context manager auto-rollbacks. Reading the file in isolation, the developer can't tell that's the safety net. A tester could reasonably write `assert mock_session.rollback.called` and find it doesn't, then "fix" it by adding explicit rollback — possibly breaking the auto-rollback path.

**Impact:** Maintainability landmine for next contributor.

**Fix:** add explicit comment in `revoke_api_key` that 404 path relies on `get_session`'s context-manager auto-rollback. Or add explicit `await session.rollback()` before raise — defensive but harmless.

### M-2. `get_or_create_by_email` is racy — admin-only is a weak excuse
**File:** `src/db/crud/users.py:22-40`
The code's own comment admits this: "Race condition on concurrent creation is tolerated... For v1 admin-only issuance this race is essentially impossible." That's true *today*, but the IntegrityError will not be a clean 422 — it'll bubble as 500 from the SQLAlchemy layer and corrupt the session (subsequent statements in the same transaction will fail with `current transaction is aborted, commands ignored`).

**Impact:** When phase 9 (compat-tests) runs concurrent admin scripts, an unlucky collision = 500 + a stuck-session that taints later requests on the same connection until the session is recycled (which may happen quickly here because of FastAPI dep, but the next CRUD call in the same handler is dead).

**Fix:** use upsert via `INSERT ... ON CONFLICT DO NOTHING RETURNING ...`:
```python
from sqlalchemy.dialects.postgresql import insert
stmt = insert(User).values(email=email).on_conflict_do_nothing(index_elements=["email"]).returning(User)
result = await session.execute(stmt)
row = result.scalar_one_or_none()
if row is not None:
    return row, True
# Conflict → fetch existing
return (await get_by_email(session, email)), False  # type: ignore[return-value]
```
KISS-acceptable today, but this is the kind of latent foot-gun that bites in tests-against-real-Postgres.

### M-3. `_authenticate` static + `Request` parameter unused
**File:** `src/gateway/middleware/auth.py:106-111`
The `request: Request` parameter is never read. The decorator `@staticmethod` + the unused arg suggests an aborted refactor. Remove the unused parameter or use it for request-id binding (would help log correlation).

**Impact:** Dead code; mypy strict ignores unused params; readability hurt.

**Fix:** drop the parameter, or pass `request.state` if you want to bind log context.

### M-4. Tests use `MagicMock` for AsyncSession — false confidence
**File:** `tests/unit/test_auth_middleware.py:42-44`, `tests/unit/test_admin_api_keys.py:56-61`
```python
async def _null_gen() -> AsyncGenerator[MagicMock, None]:
    yield MagicMock()
```
A `MagicMock` will silently accept ANY method call and return another mock. That means tests pass even if the implementation does `session.execute(...).scalar_one_or_none(...)` chained wrong, or calls `.commit()` instead of `.flush()`, etc. The `test_admin_api_keys.py:201` does pin `mock_session.execute = AsyncMock(...)` in one place, but `commit()`, `flush()`, `add()` are MagicMock no-ops — and the spec demands `await session.commit()` after key creation. There is no test that asserts commit was called.

**Impact:** "Tests green, prod fails" risk specifically for transaction commit paths. Spec §Success Criteria includes "Admin can create a key... hashed in DB" — that requires a commit; nothing tests it.

**Fix:** in `test_create_key_returns_201_with_plaintext`, add `mock_session.commit.assert_awaited_once()`. In `test_revoke_existing_key_returns_204`, same. Spec also calls for an *integration test against real Postgres* (file `tests/integration/test_admin_api_keys.py`) — currently it's a unit test under `tests/unit/` with all DB behavior mocked. The integration test from the spec is missing.

### M-5. `KEY_BYTES = 32` private but `KEY_PREFIX = "cwk_"` public — inconsistent visibility
**File:** `src/auth/hashing.py:30-31`
```python
KEY_PREFIX = "cwk_"
_KEY_BYTES = 32
```
`KEY_PREFIX` is imported by `bearer.py`. `_KEY_BYTES` is private (used only internally). That's fine. But there's no `__all__`, no `Final[str]` typing, and the prefix value is hardcoded as a string literal in tests too (`"cwk_" + "A" * 43`). If someone bumps `KEY_BYTES` to 64, the b64 length changes from 43 to 86, breaking the static-length assertion in tests. Test resilience could be improved.

**Impact:** Cosmetic + test brittleness.

**Fix:** add `Final` typing and re-use the constant in tests:
```python
from typing import Final
KEY_PREFIX: Final[str] = "cwk_"
_KEY_BYTES: Final[int] = 32
```

---

## Low / Nitpicks

### L-1. `verify_key`'s except-tuple is redundant
**File:** `src/auth/hashing.py:60`
`except (VerifyMismatchError, InvalidHashError, Exception)` — once `Exception` is in the tuple, the others are subsumed. (Same point as H-2 but documented separately for clarity.) Pick a clean three-arm except chain.

### L-2. Inline imports inside functions
**File:** `src/db/crud/api_keys.py:101, 118`
```python
from sqlalchemy import func  # noqa: PLC0415
```
This is suppressing the linter for a reason — but the reason (avoiding a name collision with the module-level `from sqlalchemy import select, update`) is non-existent: `func` is a fine top-level import. Just promote to module level. The `# noqa` makes future readers think there's a circular-import or chicken-egg reason, when really the inline-imports are leftover habit.

**Fix:** move `from sqlalchemy import func, select, update` to the module top.

### L-3. `tests/integration/test_admin_api_keys.py` does not exist; the file is at `tests/unit/test_admin_api_keys.py`
**File:** spec §Implementation Steps step 12 says "Integration test (admin flow against real Postgres) — `tests/integration/test_admin_api_keys.py`". Reality: file is in `tests/unit/`, mocks all DB calls. No real Postgres test in this phase.

**Impact:** Spec divergence. Integration test gap acknowledged in M-4 above.

**Fix:** either move + extend with a real Postgres testcontainer, or update the spec to acknowledge unit-only coverage in phase 01 with integration deferred.

### L-4. `_WRAPPER_RELEASE_TS = 1714000000` does not match docstring date
**File:** `src/gateway/routes/models.py:21-23`
```python
# 2024-04-25 00:00:00 UTC → chosen to predate first deployment.
_WRAPPER_RELEASE_TS: int = 1714000000
```
Unix `1714000000` is `2024-04-24 23:06:40 UTC` — close to but not equal to "2024-04-25 00:00:00 UTC" (which is `1714003200`). Cosmetic but a future contributor will trust the comment, change the constant to "fix" it, and break consumer cache idempotency.

**Fix:** either change the comment to "approx 2024-04-24" or the value to `1714003200`.

---

## Spec Adherence

| Step | Status | Notes |
|------|--------|-------|
| 1. Add `ADMIN_TOKEN` to settings + `.env.example` | Partial | Settings done with `SecretStr` + `prod` validator. Did not verify `.env.example` updated (out of scope of files reviewed). |
| 2. ORM models User + ApiKey | Done | Mapped[T] correct. `relationship(...lazy="noload")` is a nice touch — prevents accidental N+1. |
| 3. Migration | Done | One miss: ondelete policy not set (H-6); SQL defaults missing (H-5). |
| 4. Hashing module | Done with bug | argon2id default type confirmed (PasswordHasher uses Type.ID by default since 21.x). Plaintext format `cwk_` + 43 b64url chars confirmed. Bug: H-2/L-1 around exception handling. |
| 5. Error envelope helpers | Done | Body shape exact match to spec. `param: null` present. `internal_error_response` is a useful addition not strictly in spec but fine. |
| 6. CRUD api_keys | Done | C8 fix verified end-to-end; see "Red Team C8 fix integrity" below. C-2 candidate-cap concern. |
| 7. Bearer extractor | Done | Edge cases per H-3. |
| 8. Auth middleware | Done with bugs | C3 raw-ASGI verified. Bugs C-1 (session leak), H-1 (broad except), H-4 (skip-list overlap). |
| 9. Admin endpoint | Done | Constant-time admin compare verified. Plaintext returned once, not in LIST. |
| 10. Models endpoint | Done | Static response, owned_by="codex-wrapper". L-4 timestamp nit. |
| 11. App wiring | Done | AuthMiddleware registered after others (LIFO outer). admin_api_keys_router under `/admin` prefix. models_router on `/v1/models`. |
| 12. Tests | Partial | Hashing/middleware/models unit tests good. Admin API integration test downgraded to unit (L-3, M-4). |
| 13. Local verification | N/A | Manual smoke not reviewable from code. |

### Red Team C8 fix integrity (verified line-by-line)

**Confirmed:**
- `_BG_TASKS: set[asyncio.Task[None]] = set()` at module scope — `src/db/crud/api_keys.py:33`. ✓
- Pattern: `task = asyncio.create_task(...); _BG_TASKS.add(task); task.add_done_callback(_BG_TASKS.discard)` — `src/db/crud/api_keys.py:138-140`. ✓ Exact match.
- Background writes use `bg_session()` not `_main_session_factory()` — `src/db/crud/api_keys.py:121`. ✓
- `pool_timeout=0.5` honored: `bg_db_pool_timeout: float = 0.5` in settings, passed in `engine.py:77`. ✓
- On TimeoutError → log WARN + drop, never block request: `src/db/crud/api_keys.py:126-127`. ✓ The log-event-key is `auth.last_used.pool_timeout` — matches spec.
- Subsequent broad except logs as `auth.last_used.bg_failed` with `exc_info=True` — `src/db/crud/api_keys.py:128-129`. ✓ Note: keys's plaintext is NOT in the log (only `key_id`); redaction not strictly needed but the conservative choice was made. ✓

**One soft issue:** `except TimeoutError` — that's the built-in `asyncio.TimeoutError` (Python 3.11+ aliases this to `builtins.TimeoutError`). Pre-3.11, SQLAlchemy raises `sqlalchemy.exc.TimeoutError` which **is not** the same class. Verify deployment Python is ≥ 3.11; if you ever need to support 3.10, switch to:
```python
from sqlalchemy.exc import TimeoutError as SATimeoutError
except (TimeoutError, SATimeoutError):
```

### Red Team C3 readiness (verified)
- `class AuthMiddleware:` with `__init__(self, app)` and `async def __call__(self, scope, receive, send)` — `src/gateway/middleware/auth.py:52-104`. ✓ Pure ASGI, not BaseHTTPMiddleware.
- Docstring at file head explains the SSE-buffering rationale. ✓
- The note in spec about C3 deviation is honored. ✓

### Security correctness (per checklist)
- Argon2 type ID: confirmed (PasswordHasher default in argon2-cffi ≥ 21.x is `Type.ID`). ✓
- Plaintext format: `cwk_` + `secrets.token_bytes(32)` then b64url no padding = 47 chars. ✓ Matches spec.
- Plaintext NEVER in GET list: confirmed `ApiKeySummary` schema lacks `key`/`key_hash` field. ✓ Test asserts both absence (`test_admin_api_keys.py:213-215`). ✓
- Plaintext NEVER logged: `admin_api_keys.py:133-139` logs `key_id`, `prefix`, `tier`, `user_id` — no plaintext. ✓ Redaction processor in `observability/logging.py` would catch it anyway. ✓
- Admin token compare uses `secrets.compare_digest`: `admin_api_keys.py:102-104`. ✓
- `key_prefix` indexed: confirmed `op.create_index("ix_api_keys_prefix", ...)` — `migration:67`. ✓
- Argon2 verify on prefix-collision: code falls through; collision *tolerated*. **C-2 above** is the new concern — adversarial collision can amplify. The original red-team assessment "collisions stay ≈1" needs the cap-or-thread mitigation under attack.
- Revoked key: `where(ApiKey.revoked_at.is_(None))` in `get_active_by_prefix_and_verify` — `api_keys.py:78`. ✓
- 401 body byte-equality vs OpenAI: spec body is `{"error":{"message":"Incorrect API key provided.","type":"invalid_request_error","param":null,"code":"invalid_api_key"}}`. Implementation returns `{"error":{"message":"Incorrect API key provided.","type":"invalid_request_error","param":null,"code":"invalid_api_key"}}` — byte-identical (key order is JSON-dict-order which Python 3.7+ preserves; FastAPI's JSONResponse does not reorder). ✓ However, **no test asserts byte-equality of the full body**; tests only check field presence. Adding `assert body == {expected_dict}` would harden this.

### DB / migration correctness
- `users.email` UNIQUE: ✓ (migration:44), and ORM `unique=True`. Index implicit via UNIQUE on Postgres.
- `api_keys.key_hash` UNIQUE: ✓ (migration:65). But: argon2 hashes include random salt; per-plaintext hashes always differ. UNIQUE is essentially free (no functional collision possible) but technically allows `key_hash` to be the dedupe primary candidate. Fine.
- `api_keys.prefix` indexed non-unique: ✓.
- FK `api_keys.user_id` → `users.id`: ✓ but no ondelete (H-6).
- `op.create_index` correctly used: ✓.
- `downgrade()` implemented (not `pass`): ✓ — drops index, then table, then table.
- SQLAlchemy 2.0 `Mapped[T]` syntax: ✓ throughout.

### Auth middleware correctness
- Bypass list exact match for `/healthz`, `/readyz`, `/metrics`: ✓.
- `/admin/` prefix bypass: ✓ (acceptable given X-Admin-Token is checked at the route layer).
- `OPTIONS` (CORS preflight) NOT explicitly bypassed → will hit auth and 401. Phase 01 has no CORS scope, so this is fine; flag for phase 9 SDK compat tests. Browser-based clients will fail until CORS middleware is added.
- DB-lookup exception → 500 (not 401): ✓ via `internal_error_response`. Avoids info-leak.
- request.state populated on success: ✓ `api_key_id`, `user_id`, `tier`. ✓
- Multiple Bearer headers: not handled — `headers.get("authorization")` returns the first value. Acceptable per RFC.
- Header parsing: handles standard "Bearer <token>"; H-3 covers edge cases.

### Admin route correctness
- POST returns plaintext + key id; subsequent GET filters them out: ✓.
- Constant-time admin compare: ✓.
- Admin endpoints under `/admin/` prefix not `/v1`: ✓.
- Error responses: admin returns FastAPI default `{"detail":"..."}` for HTTPException(403). This is **not** the OpenAI-shape envelope. Spec §Implementation step 9 says "403 `permission_denied` on mismatch" — implementation does that with `HTTPException(status_code=403, detail="permission_denied")` which yields `{"detail":"permission_denied"}`. The auth-middleware errors are OpenAI-shaped; admin errors are not. **Inconsistent**, but admin endpoint is for human operators so probably fine. Worth a one-line spec note that admin uses default FastAPI shape.

### Type safety
- `ph.type` (mypy): no obvious `# type: ignore` masking real issues.
- `_authenticate` returns `Any` (commented `# returns ApiKey | None`) — could be tightened to `ApiKey | None` once C-1 is fixed.
- `dict[str, object]` on `list_models` is technically correct but loose; consider a TypedDict or pydantic model for clarity.

### Testing quality
- Hashing tests: argon2 verify failure modes covered (mismatch, corrupted hash, empty plaintext). ✓
- `_BG_TASKS` reference holding: NOT tested. Spec C8 mitigation is the central red-team fix; absence of "task survives a GC pass" test means the only proof is code review. Recommend adding:
  ```python
  async def test_bg_task_held_during_execution():
      task_started = asyncio.Event()
      task_done = asyncio.Event()
      async def slow():
          task_started.set()
          await asyncio.sleep(0.1)
          task_done.set()
      task = asyncio.create_task(slow())
      _BG_TASKS.add(task)
      task.add_done_callback(_BG_TASKS.discard)
      await task_started.wait()
      gc.collect()
      assert task in _BG_TASKS
      await task_done.wait()
      await asyncio.sleep(0)  # let done_callback fire
      assert task not in _BG_TASKS
  ```
- Revoked-key flow end-to-end: NOT tested. The CRUD-level `revoke()` test (M-4 mock) doesn't test that subsequent middleware lookup returns None. A two-phase test (create → auth-200 → revoke → auth-401) is the success criterion in spec — missing.
- 401 body byte-equality vs OpenAI: NOT exact-equality tested. Field presence only.
- BG session timeout simulation: NOT tested. The C8 drop-on-pool-exhaustion path has no test coverage. Mock `bg_session` to raise `TimeoutError` and assert the warn log fires + no exception propagates.

### YAGNI/DRY/KISS
- `internal_error_response()` was added beyond spec (spec only listed `invalid_api_key` and `permission_denied`). Justified — middleware exception path needs SOMETHING to return; reusable.
- `permission_denied_response` is exported but not used (admin uses raw HTTPException). Minor DRY violation — admin route should reuse `permission_denied_response` for consistency, OR delete the helper.
- `get_by_id` in CRUD is unused in this phase (revoke uses bulk UPDATE w/ RETURNING). YAGNI says delete; spec doesn't list it. Probably fine to keep for phase 8 admin GET-by-id, but flag the intent in the docstring or remove now.
- Session/dep boilerplate is reasonable; no significant duplication.

---

## Strengths

- **Clean module boundaries** — every file < 200 LOC, well within the cap. `auth/`, `db/crud/`, `gateway/middleware/`, `gateway/routes/` separation is textbook.
- **Docstrings explain *why*, not *what*** — particularly the C3 (raw ASGI) and C8 (bg pool) rationales at file-head.
- **Constant-time compares everywhere** — argon2 verify (built-in) and admin token (`secrets.compare_digest`).
- **Plaintext discipline** — generated, returned once, never logged, never in LIST. Redactor is belt-AND-suspenders.
- **Default-deny by middleware design** — adding any new `/v1/*` route automatically inherits auth requirement; spec §Security explicitly calls this out and the implementation honors it.
- **`relationship(..., lazy="noload")`** — defensive against accidental N+1 in admin routes.
- **`expire_on_commit=False`** in both session factories — caller can read attributes after commit without re-fetch (matters for `admin_api_keys.py:141-147` where `api_key.created_at` is accessed post-commit).
- **`pool_pre_ping=True`** in both engines — silently recovers from idle-killed connections in production.
- **Test hygiene** — every test file has clear docstring listing what's covered. The PEP-563-vs-FastAPI-annotations note is great institutional knowledge.
- **The C8 implementation is exactly the spec** — _BG_TASKS module-level set, add+discard pattern, separate bg pool, 0.5s timeout drop, structlog WARN. Word-for-word from `phase-01-auth-and-models.md` §6.

---

## Recommended Actions (priority-ordered)

1. **Fix C-1**: replace `async for session in get_session(): return ...` with a direct `async with main_session() as session: ...` in the middleware. Add a `main_session()` helper to `engine.py` mirroring `bg_session()`. (1 hour)
2. **Fix C-2**: cap candidates to `.limit(2)`, return None on >= 2; offload argon2 to `asyncio.to_thread`. (30 min)
3. **Fix H-2**: rewrite `verify_key` with three-arm except (mismatch silent, invalid-hash logged, others propagate). (15 min)
4. **Fix H-1**: branch `sqlalchemy.exc.TimeoutError` and `OperationalError` in middleware exception handler with distinct log-event-keys. (15 min)
5. **Fix H-4**: tighten skip-list to exact paths + only critical prefixes. Add unit test that `/healthz/sub` requires auth. (30 min)
6. **Add missing tests**: byte-equality of 401 body, BG task GC survival, revoked-key end-to-end flow, BG session timeout simulation, `commit()` assertions on admin POST/DELETE. (1 hour)
7. **Fix M-2**: `get_or_create_by_email` → ON CONFLICT DO NOTHING upsert. (20 min)
8. **Fix H-5/H-6**: migration adds `server_default` for `id` + `tier`, explicit `ondelete="RESTRICT"` on FK, `EmailStr` lowercase normalize. (30 min)
9. **Fix H-3, L-1, L-2, L-4**: minor cleanups. (30 min combined)
10. **Move/create `tests/integration/test_admin_api_keys.py`**: real Postgres via testcontainers OR document the deferral explicitly in the spec. (45 min if doing it; 5 min if deferring with note)

**Total estimated rework: ~5 hours.** All fixes are local; none require schema changes beyond the migration tweak which can ship in a follow-up `0003` migration if `0002` has already been applied somewhere.

---

## Metrics
- Type Coverage: high (mypy strict survives based on inspection; no `# type: ignore` masking real issues except documented one in `engine.py`).
- Test Coverage (file presence): hashing ✓, middleware ✓, models ✓, admin ✓ (unit-only). Integration ✗.
- Linting Issues: known `noqa: PLC0415` × 2 (L-2). No other obvious lint smells.
- File-size compliance: all 11 phase-01 source files ≤ 189 LOC. ✓

---

## Unresolved Questions

1. **Argon2 thread offload**: argon2-cffi releases the GIL during the C call, so `asyncio.to_thread` is genuinely concurrent. But it pays a thread context-switch cost (~50µs). Is the per-request 50µs overhead acceptable, OR should we just live with the event-loop-pin and rely on CPU-count-1 horizontal scaling? Recommend: do the thread offload anyway because event-loop-pinning of 25-50ms blocks ALL other async work on that worker, including SSE keepalive frames in phase 03.

2. **Admin endpoint error shape**: should `/admin/api-keys` errors use OpenAI envelope (`{"error":{...}}`) or default FastAPI shape (`{"detail":"..."}`)? Spec says "OpenAI-shape" only for the bearer-auth path; admin is silent. Decision should be documented.

3. **Integration test scope**: spec says `tests/integration/test_admin_api_keys.py` against real Postgres. Reality: file in `tests/unit/`, all DB mocked. Either move it (real Postgres needs testcontainers in CI) or update spec to acknowledge unit-only for this phase.

4. **Email normalization**: pydantic `EmailStr` in 2.x does not lowercase. Should `user_email` be lowercased at the API boundary, or is "Alice@x.com vs alice@x.com being separate users" tolerable for v1 admin-only flow?

5. **Python version**: confirm runtime is ≥ 3.11 so `except TimeoutError` catches both built-in and asyncio variants. Pre-3.11, SQLAlchemy's `pool_timeout` raises `sqlalchemy.exc.TimeoutError` which is unrelated.

---

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical count:** 2
