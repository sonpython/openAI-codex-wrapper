# Code Review — codex-execution-modes (P1+P2+P3)

**Verdict:** READY_WITH_CONCERNS — ship core, but fix doc lie + add 1 defensive guard.

**Score:** 9.0 / 10

---

## Top issues (ranked)

### 1. HIGH — Docs reference admin endpoint that doesn't exist
`docs/operations-runbook.md:502-506` documents:
```bash
curl -s -X PATCH https://<gateway>/admin/api-keys/abc123 \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -d '{"mode": "vps"}'
```
**No such route exists.** `src/gateway/routes/admin_api_keys.py` exposes only `POST`, `GET`, `POST .../rotate`, `DELETE`. The Admin UI also has no edit-mode form. Operators following this runbook will get 405. The only ways to flip mode are: (a) recreate the key, (b) hand-edit Postgres.
- **Fix A (preferred):** add `PATCH /admin/api-keys/{id}` (mode-only) + matching UI control. Keep rotate response schema in sync (it currently omits `mode` while create responds with it — minor inconsistency).
- **Fix B (faster):** delete the PATCH example from runbook §"Per-API-Key Execution Modes"; replace with a `psql` UPDATE snippet matching reality.

### 2. HIGH — Authz gap: `/v1/codex/jobs` body.mode bypasses api_keys.mode
`src/gateway/routes/jobs.py:65-76` only short-circuits on `local-bridge`. After that it accepts whatever `body.mode ∈ {"read-only","workspace-write"}` the caller sent (`src/gateway/schemas/jobs.py:37`). Worker (`src/workers/job_handlers.py:137`) maps `body.mode == "workspace-write"` → `--sandbox workspace-write` directly — never consults `api_keys.mode`.

Concrete attack: a `sandbox`-tier key holder calls `POST /v1/codex/jobs {mode:"workspace-write",...}` and gets a workspace-write codex run. The blast radius is the cloned repo dir under `/workspaces/{job_id}/repo` (Landlock confines), so it's bounded — but the api_keys.mode gating pretense is broken.

- **Fix:** in `create_job()` reject `body.mode == "workspace-write"` when `request.state.codex_mode == "sandbox"`. Pseudocode:
  ```python
  if body.mode == "workspace-write" and api_mode == "sandbox":
      raise HTTPException(403, detail={"error":{"type":"permission_denied","code":"mode_not_allowed",...}})
  ```
  Document the mapping: jobs need `api_keys.mode ∈ {vps}` for `workspace-write` (or relax once jobs gain its own per-job approval flow).

### 3. MEDIUM — Unhandled `ValueError` from `resolve_sandbox_flag` leaks 500 on unknown mode
`src/codex/runner.py:72-88` raises `ValueError` for any mode not in the static map. Routes (`chat_completions.py:103`, `responses.py:102`) call it without try/except after only filtering `local-bridge`. If a future migration adds `"hybrid"` to the CHECK constraint *before* the code map is updated, every authenticated request from such keys 500s with a stack trace through FastAPI's default handler — leaking internal trace lines if `debug=True` ever flips.

- **Fix:** wrap in try/except `ValueError` and return a clean 501/500 OpenAI-shaped envelope:
  ```python
  try:
      sandbox_flag = resolve_sandbox_flag(api_mode)
  except ValueError:
      logger.warning("chat.unsupported_mode", api_mode=api_mode)
      return _openai_error(501, f"mode {api_mode!r} not supported by this gateway", error_type="api_error", code="mode_not_supported")
  ```
  Or extend the route's existing `local-bridge` short-circuit to the broader "not in `_API_MODE_TO_CODEX_SANDBOX`" check.

### 4. MEDIUM — 501 envelope shape diverges between routes
`chat_completions.py:97-102` and `responses.py:96-101` use `_openai_error()` which sets `param: None` (matches OpenAI envelope). `jobs.py:66-76` hand-rolls a JSONResponse and **omits** the `param` field:
```python
{"error": {"type": "api_error", "code": "local_bridge_not_implemented", "message": "..."}}
```
Strict OpenAI clients (Open WebUI, HA Extended OpenAI) parse `error.param` as a known optional — missing key is fine — but inconsistency causes confusion in tests and docs and risks one future SDK breaking.
- **Fix:** route `jobs.py` 501 through a small helper or include `"param": None` explicitly.

### 5. LOW — File hygiene (tracked, not blocking)
LOC counts after this work:
- `src/codex/runner.py` 290 (was already over; adds ~20)
- `src/workers/job_handlers.py` 304 (untouched LOC; signature adapt only)
- `src/gateway/routes/jobs.py` 329 (untouched by P2 except mode 501 block)
- `src/gateway/routes/admin_api_keys.py` 279

These exceed the project's 200 LOC guideline but are pre-existing; this PR didn't push any over the line meaningfully. Recommend a follow-up split (e.g. extract job_handlers' codex-loop section, extract jobs.py route handlers per verb) — not in this PR's scope.

### 6. LOW — `responses` `sent_terminal` detection misses `response.cancelled`
`src/gateway/routes/responses.py:156` only detects `response.completed` / `response.failed` as terminal markers. `response.cancelled` is also a terminal event emitted by `stream_responses` cancel branch. Today this is benign because the cancel path raises CancelledError after yielding, which bypasses the `except Exception` (CancelledError is BaseException-derived in 3.8+). But if anyone refactors stream_handler to swallow cancellations, the wrapper would synthesize a duplicate `response.failed` after `response.cancelled` was already emitted.
- **Fix:** broaden the prefix tuple to `(b"event: response.completed", b"event: response.failed", b"event: response.cancelled")`. Trivial.

---

## What I tried to break and couldn't

- **Bypass local-bridge 501 by manipulating request body** — body shape doesn't influence dispatch; mode comes from middleware-stashed `request.state.codex_mode`. Caller can't control it via JSON. ✓
- **Admin pydantic vs DB CHECK constraint mismatch** — `field_validator` in `AdminCreateKeyRequest` rejects unknown modes at 422 before DB; CRUD `create()` re-validates against `VALID_MODES`; DB CHECK is third belt. Three layers of agreement. ✓
- **TOCTOU between auth read and route use** — `api_key.mode` is read into `request.state` once. Concurrent revocation/mode-change does not affect in-flight request. Acceptable; auth has already passed.
- **Argv drift `mode=sandbox` vs pre-Phase-2** — diffed `git show HEAD:src/codex/runner.py` vs current. Argv list is identical for `sandbox` → `read-only`; only the parameter name changed (`allow_write` → `sandbox_mode`). Backward-compat preserved.
- **Migration race during ADD COLUMN/ADD CONSTRAINT** — both ops are inside a single Alembic `upgrade()` and PostgreSQL DDL is transactional, so the constraint is in place atomically with the column. No mid-flight illegal inserts possible.
- **Existing rows after `alembic upgrade`** — `server_default 'sandbox'` on a NOT NULL ADD COLUMN backfills existing rows in PG 11+ in O(1) without rewrite. ORM `default="sandbox"` matches DB default; both read and write paths agree. ✓
- **Double `[DONE]` on happy path** — covered by `test_stream_success_done_emitted_exactly_once` and `test_responses_stream_success_no_double_terminal`. wrapper's `sent_done`/`sent_terminal` flag detects sentinel before raising path. ✓
- **Stack-trace leak from synth_error_chunk / synth_failed_event** — both helpers only emit fixed payloads (id/model/created and id/object/status); no stderr or trace leakage. ✓
- **Env leak via mode dispatch error** — `local-bridge` route returns canned message ("local-bridge mode is not yet implemented") with no internal context. ✓
- **Unauthenticated bypass of /v1/* paths** — bearer middleware default-denies; AUTH_SKIP_PATHS does not include /v1. ✓
- **`--cd` confinement claim in vps mode** — runbook §Mode-risk-summary correctly flags this as "container is the only isolation boundary"; no false marketing.
- **Dead `allow_write` plumbing** — `grep -rn "allow_write" src tests` returns no matches. Fully removed. ✓

---

## Test coverage gaps

| Need | Status |
|---|---|
| `mode=sandbox` argv = pre-Phase-2 argv (snapshot diff) | ❌ no explicit snapshot, but `test_argv_sandbox_read_only` + manual diff proves equivalence; nice-to-have |
| `mode=vps` invokes `--cd <workspace>` | ✓ covered by `test_argv_color_never_pair_unbroken` (asserts cd_idx, prompt_idx ordering) + `test_argv_sandbox_danger_full_access` |
| Happy-path `[DONE]` emitted exactly once | ✓ `test_stream_success_done_emitted_exactly_once` |
| Worker reads `api_keys.mode` at job pickup | ❌ **N/A by design** — worker uses `job.mode` not `api_keys.mode` (independent column). Authz gap #2 above is the actual concern; once fixed there'll need a route-level test |
| `local-bridge` 501 short-circuit before workspace creation | ✓ `test_local_bridge_mode_returns_501_*` (asserts runner not called) |
| Stream wrapper exception → finish_reason='error' + [DONE] | ✓ Phase-3 tests |
| Wrapper exception does NOT propagate | ✓ `test_*_does_not_propagate` |

---

## Positive observations

- Three-layer mode validation (pydantic → CRUD → DB CHECK) is textbook defense-in-depth.
- Migration is reversible and atomic; downgrade order (drop constraint, then drop column) is correct.
- SSE wrapper correctly uses `try/except/finally` around generator; `sent_done`/`sent_terminal` flags are guarded against double-emit on both happy and error paths; finally-block always populates `request.state.usage` for downstream middleware.
- `synth_error_chunk` / `synth_failed_event` helpers are minimal, leak-free, and isolated to dedicated modules.
- `resolve_sandbox_flag` raising on unmapped mode is good defense-in-depth — but caller must catch (see issue #3).
- Constants (`VALID_MODES`, `DEFAULT_MODE`) are single-source-of-truth in `src/db/crud/api_keys.py` and reused by all consumers (admin REST, admin UI). DRY win.

---

## Recommended actions (in order)

1. **Fix #1**: delete or implement the documented `PATCH /admin/api-keys/{id}` for mode change. Pick one, ship.
2. **Fix #2**: gate `body.mode=workspace-write` on `api_keys.mode ∈ {vps}` in `create_job`. Add a unit test.
3. **Fix #3**: wrap `resolve_sandbox_flag()` calls in try/ValueError → 501/500 OpenAI envelope.
4. **Fix #4**: normalize jobs.py 501 envelope to include `param: None` (or use `_openai_error` helper).
5. **Fix #6**: add `b"event: response.cancelled"` to the terminal-prefix tuple. Trivial.
6. (Future) split `runner.py` (mode helpers + run loop), `job_handlers.py` (lifecycle vs codex loop), `jobs.py` (per-verb routers) — not blocking.

---

## Metrics

- Type coverage: clean (mypy --strict per project standard, no new ignores observed).
- Lint: clean (`ruff check` passes on all touched files).
- Tests: 104/104 passed in scoped run (admin_api_keys + runner + chat + responses + jobs routes).
- LOC delta: +1086 / -82 (mostly tests + docs).

---

## Unresolved questions

1. Is the per-API-key mode supposed to gate the `/v1/codex/jobs` body.mode field (issue #2)? If yes, blocking. If "no — jobs are independent," need an explicit doc statement and audit log on workspace-write submissions.
2. The PATCH /admin/api-keys endpoint in runbook — was this ever planned or is it a doc draft that leaked? Need product call: implement vs delete.
3. Phase-4 deploy task #25 still pending — confirm 192.168.1.120 codex 0.125 actually accepts `--sandbox danger-full-access` literally (risk gate #26 marked complete; spot-check log artifact?).
