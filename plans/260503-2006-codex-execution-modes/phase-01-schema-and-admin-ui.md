---
title: "Phase 1 — Schema + admin UI mode dropdown"
status: completed
priority: P1
effort: 2h
blocks: [phase-02]
blocked_by: []
completed: 2026-05-04
---

# Phase 1 — Schema + admin UI mode dropdown

## Context Links

- Source brainstorm: `plans/reports/brainstorm-260503-2006-codex-execution-modes.md`
- Existing migration pattern: `src/db/migrations/versions/20260427_0002_users_and_api_keys.py`
- Existing tier flow: `src/admin_ui/keys_page_routes.py`, `src/admin_ui/templates/keys.html`, `src/admin_ui/templates/partials/keys_row.html`
- Admin POST endpoint to mirror: `src/gateway/routes/admin_api_keys.py` (`AdminCreateKeyRequest`)

## Overview

Add `api_keys.mode` enum column. Surface it in admin UI key creation form + key row. Mirror the same enum in admin REST endpoint and CRUD helper.

## Key Insights

- `tier` already follows the exact pattern we want — reuse the wire (Form field + dropdown + crud arg + ApiKey constructor).
- Default `sandbox` keeps every existing key bit-identical — non-breaking.
- `local-bridge` UI option must be **rendered but disabled** with a "coming soon" hint to advertise direction without wiring.
- Migration must include CHECK constraint so a typo at the DB layer fails fast.

## Requirements

### Functional

- [ ] Alembic migration adds `mode VARCHAR(16) NOT NULL DEFAULT 'sandbox'` with CHECK constraint `mode IN ('sandbox','vps','local-bridge')`.
- [ ] ORM `ApiKey` model exposes `mode: Mapped[str]` with same default.
- [ ] `crud.api_keys.create()` accepts `mode` kwarg (default `"sandbox"`).
- [ ] `POST /admin/api-keys` accepts `mode` field with enum validator.
- [ ] Admin UI key creation modal shows new "Mode" dropdown (sandbox / vps / local-bridge — last `disabled`).
- [ ] Key row template displays mode badge alongside tier.
- [ ] Admin UI POST handler `/keys/_create` validates + forwards `mode`.
- [ ] Existing GET handlers expose `mode` in row dict.

### Non-Functional

- [ ] No file > 200 LOC after edits.
- [ ] Migration is reversible (drop column + drop check).
- [ ] No new dependencies.

## Architecture

### Data flow

```
Admin UI form (modal)
  └─ POST /admin/ui/keys/_create  (Form field: mode)
      └─ keys_page_routes.post_create_key()  (validate _VALID_MODES)
          └─ api_keys_crud.create(... mode=...)
              └─ INSERT api_keys(...mode...)
```

### Enum

Single source of truth: a module-level constant.

```python
# src/db/crud/api_keys.py
VALID_MODES: frozenset[str] = frozenset({"sandbox", "vps", "local-bridge"})
DEFAULT_MODE: str = "sandbox"
```

Re-imported by both admin endpoints (REST + UI) — DRY.

## Related Code Files

### Create

- `src/db/migrations/versions/20260503_0010_api_keys_mode_column.py` — new migration.

### Modify

- `src/db/models.py` — add `mode` column to `ApiKey`.
- `src/db/crud/api_keys.py` — `create()` accepts `mode`; export `VALID_MODES`, `DEFAULT_MODE`.
- `src/gateway/routes/admin_api_keys.py` — `AdminCreateKeyRequest` adds `mode` + `validate_mode`. `AdminCreateKeyResponse` + `ApiKeySummary` expose `mode`.
- `src/admin_ui/keys_page_routes.py` — `_VALID_MODES` constant; `_build_key_dict` returns `mode`; `post_create_key` accepts Form `mode` and forwards.
- `src/admin_ui/templates/keys.html` — add Mode `<select>` to create modal; add Mode column header.
- `src/admin_ui/templates/partials/keys_row.html` — add Mode badge cell.
- `tests/unit/test_admin_api_keys.py` — extend with mode validation cases.
- `tests/unit/test_admin_ui_keys_tiers_routes.py` — extend with mode field cases.

### Do not touch

- Auth middleware (deferred to Phase 2 — needs mode plumbed into request.state).

## Implementation Steps

1. **Migration `0010_api_keys_mode_column.py`**
   - `revision='0010'`, `down_revision='0009'`.
   - `upgrade()`:
     - `op.add_column("api_keys", sa.Column("mode", sa.String(length=16), nullable=False, server_default=sa.text("'sandbox'")))`
     - `op.create_check_constraint("ck_api_keys_mode", "api_keys", "mode IN ('sandbox','vps','local-bridge')")`
   - `downgrade()`: drop check, drop column.
   - Postgres-only check (skip on SQLite for tests — mirror pattern from `0002` `pgcrypto` guard if needed; keep simple — sa.String with check works on both).
2. **`src/db/models.py`** — add `mode: Mapped[str] = mapped_column(String(16), nullable=False, default="sandbox", server_default=text("'sandbox'"))` and update `__repr__`.
3. **`src/db/crud/api_keys.py`** — define `VALID_MODES`, `DEFAULT_MODE`; extend `create()` signature with `mode: str = DEFAULT_MODE`; pass to `ApiKey(...)`. Reject invalid mode early with `ValueError`.
4. **`src/gateway/routes/admin_api_keys.py`** — import `VALID_MODES, DEFAULT_MODE`. Extend `AdminCreateKeyRequest` (`mode: str = DEFAULT_MODE`) with `validate_mode` field validator. Expose `mode` in `AdminCreateKeyResponse` + `ApiKeySummary`. Pass `mode=body.mode` to `crud.create()`.
5. **`src/admin_ui/keys_page_routes.py`** — same imports. Add `mode: Annotated[str, Form()] = "sandbox"` to `post_create_key`. Validate via `VALID_MODES`. Update `_build_key_dict` to include `mode`. Forward to crud.
6. **`src/admin_ui/templates/keys.html`** — under tier select, add:
   ```html
   <div>
     <label class="block text-sm font-medium text-gray-700 mb-1">Mode</label>
     <select name="mode" class="...">
       <option value="sandbox">Sandbox (read-only)</option>
       <option value="vps">VPS (full access)</option>
       <option value="local-bridge" disabled>Local-bridge (coming soon)</option>
     </select>
   </div>
   ```
   Add `<th>Mode</th>` to thead between Tier and User.
7. **`src/admin_ui/templates/partials/keys_row.html`** — add `<td>` for mode badge between tier and user_email cells:
   ```html
   <td class="px-4 py-3">
     <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium
       {% if key.mode == 'sandbox' %}bg-gray-100 text-gray-600
       {% elif key.mode == 'vps' %}bg-emerald-100 text-emerald-700
       {% else %}bg-yellow-100 text-yellow-700{% endif %}">{{ key.mode }}</span>
   </td>
   ```
   Bump existing `colspan="8"` empty-state to `colspan="9"`.
8. **Tests**
   - `test_admin_api_keys.py`: parametrize `mode` ∈ valid set; assert created row has correct mode; assert invalid mode → 422.
   - `test_admin_ui_keys_tiers_routes.py`: POST `_create` with `mode=vps` → returns row with `vps` badge.
9. **Compile check** — `python -c "from src.db.models import ApiKey; print(ApiKey.__table__.columns.keys())"` and `alembic upgrade head` against test DB.

## Todo List

- [x] Write migration 0010
- [x] Update ORM model
- [x] Extend crud helper + constants
- [x] Extend REST admin endpoint schema + handler
- [x] Extend admin UI route handler
- [x] Update admin UI templates (form + row + thead)
- [x] Add unit tests (REST + UI)
- [x] Run `alembic upgrade head` and `pytest tests/unit -q`
- [x] Verify downgrade is clean (`alembic downgrade -1` then `upgrade head`)

## Success Criteria

- [x] `alembic upgrade head` applies cleanly on fresh DB and on existing DB at rev 0009.
- [x] All existing rows have `mode='sandbox'` after migration.
- [x] `POST /admin/api-keys {"mode":"vps",...}` returns 201 with `mode` in body.
- [x] `POST /admin/api-keys {"mode":"bogus",...}` returns 422.
- [x] Admin UI screenshot/manual: dropdown shows three options, `local-bridge` disabled.
- [x] All unit tests green.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| CHECK constraint blocks legacy rows | Low | High | Default `'sandbox'` covers existing rows; verify with `SELECT COUNT(*) WHERE mode IS NULL` after upgrade. |
| Template colspan drift breaks empty-state row | Med | Low | Bump explicitly in step 7; cover in UI route test. |
| Pydantic v2 enum validator drifts vs `tier` pattern | Low | Low | Copy-paste tier validator verbatim; replace constant. |
| Breaking change for admin REST clients | Low | Med | `mode` is optional with default; no consumer breakage. |

## Security Considerations

- `mode` field is admin-only (already guarded by X-Admin-Token + session auth).
- Enum validation prevents arbitrary string injection.
- No PII implications.

## Next Steps

- Phase 2 reads `mode` from `request.state` → runner. Schema must land first so middleware can populate it.
