---
phase: 2
title: "Key CRUD + Tier Editor"
status: pending
priority: P2
effort: "1d"
dependencies: [1]
---

# Phase 2: Key CRUD UI + Tier Editor

## Overview

UI cho API key management (issue / list / rotate / revoke) và Plan tier editor (rpm/tpm/concurrent/monthly per tier). Reuse existing `admin_api_keys.py` endpoints; add `admin_tiers.py` GET/PUT.

## Requirements

- Functional:
  - `/admin/ui/keys`: table list keys (prefix, tier, user_email, created_at, last_used) + Create button → modal form (user_email, name, tier select) → POST `/v1/admin/api-keys` → display raw key 1 lần với copy button + warning
  - Per row actions: Rotate (confirm modal → POST rotate → display new raw key), Revoke (confirm modal → DELETE)
  - HTMX swap rows in-place sau create/rotate/revoke; toast notification cho errors
  - `/admin/ui/tiers`: table 4 tiers (free/pro/team/enterprise) với cells inline-edit (rpm, tpm, concurrent, monthly_quota); Save button per row → PUT `/v1/admin/tiers/{tier}` → invalidate_cache() server-side → toast confirm
- Non-functional:
  - Raw API key shown ONCE; no DB column for raw value
  - All admin actions logged to `audit_log` (existing middleware)
  - Tier edit atomic: validate ranges (rpm > 0, monthly_quota >= 0) before commit

## Architecture

```
[/admin/ui/keys]                   [/admin/ui/tiers]
  │                                   │
  ├─ GET → load existing GET /v1/admin/api-keys
  │                                   ├─ GET /v1/admin/tiers (NEW)
  ├─ POST modal → POST /v1/admin/api-keys
  ├─ Rotate → POST /v1/admin/api-keys/{id}/rotate
  └─ Revoke → DELETE /v1/admin/api-keys/{id}
                                      └─ Save row → PUT /v1/admin/tiers/{tier} (NEW)
                                          └─ crud_plans.update() + invalidate_cache()
```

New endpoint `admin_tiers.py`:
- `GET /v1/admin/tiers` returns list of `{tier, rpm, tpm, concurrent, monthly_quota}`
- `PUT /v1/admin/tiers/{tier}` body `{rpm, tpm, concurrent, monthly_quota}` → upsert + invalidate_cache()

## Related Code Files

- Create:
  - `src/gateway/routes/admin_tiers.py` — GET list + PUT update
  - `src/db/crud/plans.py` — extend with `update(session, tier, payload)` if not present
  - `src/admin_ui/templates/keys.html`
  - `src/admin_ui/templates/_keys_row.html` — partial cho HTMX swap
  - `src/admin_ui/templates/_keys_create_modal.html`
  - `src/admin_ui/templates/tiers.html`
- Modify:
  - `src/admin_ui/routes.py` — add page handlers GET `/keys`, GET `/tiers` + HTMX action handlers proxy to data API
  - `src/gateway/app.py` — include `admin_tiers` router

## Implementation Steps

1. Extend `src/db/crud/plans.py` với `async def update(session, tier, rpm, tpm, concurrent, monthly_quota)`:
   - `INSERT … ON CONFLICT (tier) DO UPDATE`
   - Validate values >= 0
2. Create `src/gateway/routes/admin_tiers.py`:
   - `GET /v1/admin/tiers` → list all Plans
   - `PUT /v1/admin/tiers/{tier}` body Pydantic → call crud.update → call `invalidate_cache()` from `src/db/crud/plans.py` → return updated row
3. Pydantic schemas: `TierEdit(rpm: int, tpm: int, concurrent: int, monthly_quota: int)` với `Field(ge=0)`
4. Add page handler `/admin/ui/keys` in `routes.py`:
   - GET → call existing list endpoint server-side (httpx with X-Admin-Token from settings) → render `keys.html`
   - HTMX POST `/admin/ui/keys/_create` → proxy to `/v1/admin/api-keys` → return `_keys_row.html` partial
   - HTMX POST `/admin/ui/keys/{id}/_rotate` → proxy → return updated row partial
   - HTMX DELETE `/admin/ui/keys/{id}` → proxy → return empty (HTMX removes row)
5. Add page handler `/admin/ui/tiers`:
   - GET → list tiers → render `tiers.html` table with editable cells
   - HTMX PUT `/admin/ui/tiers/{tier}/_save` → proxy → return success toast partial
6. Templates: minimal Tailwind cards/tables, modals via HTMX `hx-target="#modal"` + `hx-swap="innerHTML"`
7. Tests:
   - Unit: `admin_tiers.py` PUT validates payload, calls invalidate_cache
   - Integration: edit tier → next chat completion sees new rate limit (verify via mocked Redis)
   - UI: HTMX endpoints return correct partials

## Success Criteria

- [ ] Issue key shows raw key with copy button; raw never in DOM after first display
- [ ] Revoke removes key + rejects subsequent requests with that key
- [ ] Rotate generates new raw key + invalidates old
- [ ] Tier edit reflects within next gateway request (cache invalidated)
- [ ] All actions logged to `audit_log`
- [ ] 10+ unit tests new endpoints
- [ ] Integration test: tier edit → request rate-limit changes

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Raw key leaked via browser back-button | Render in modal, blur on close, no GET endpoint returns raw key after creation |
| Tier edit breaks rate-limit middleware | Atomic: commit DB → invalidate cache in same request; test with mocked Redis |
| Invalid tier name (e.g. "free2") allowed | Constrain via DB CHECK or whitelist in PUT schema |
| Concurrent admin edits race | Last-write-wins acceptable v1; future: `If-Match` etag |
| Migration: monthly_quota column may not exist | Verify schema in migration `0004_plans.py`; if missing → defer monthly edit to future or add migration (NOT this phase per constraint) |
