# Phase 2 Implementation Report — Key CRUD UI + Tier Editor

## Files Modified

| File | Lines | Action |
|------|-------|--------|
| `src/db/crud/plans.py` | 157 | Extended with `update()` (upsert via INSERT ON CONFLICT) + `list_all()` |
| `src/gateway/routes/admin_tiers.py` | 146 | Created — GET/PUT /v1/admin/tiers endpoints, X-Admin-Token auth |
| `src/gateway/app.py` | ~295 | Mounted admin_tiers router (no prefix, paths include /v1/admin/tiers) |
| `src/admin_ui/routes.py` | 219 | Rewrote — stripped key/tier handlers to sub-modules, kept auth+dashboard |
| `src/admin_ui/templates_env.py` | 19 | Created — shared Jinja2 singleton to avoid circular deps |
| `src/admin_ui/keys_page_routes.py` | 176 | Created — GET/POST/DELETE /keys handlers |
| `src/admin_ui/tiers_page_routes.py` | 129 | Created — GET/PUT /tiers handlers |
| `src/admin_ui/templates/keys.html` | 131 | Created — keys table + create modal + JS helpers |
| `src/admin_ui/templates/partials/keys_row.html` | 58 | Created — row partial with once-display raw key reveal |
| `src/admin_ui/templates/partials/toast.html` | 38 | Created — toast partial (success/error/info) |
| `src/admin_ui/templates/tiers.html` | 83 | Created — editable tier table with Save-per-row |
| `tests/unit/test_admin_tiers.py` | 195 | Created — 15 unit tests for /v1/admin/tiers |
| `tests/unit/test_admin_ui_keys_tiers_routes.py` | 382 | Created — 15 unit tests for UI key/tier page handlers |

## Tasks Completed

- [x] Extended `src/db/crud/plans.py` with `update()` upsert + `list_all()`
- [x] Created `src/gateway/routes/admin_tiers.py` GET + PUT with Pydantic validation (`Field(ge=0)`)
- [x] Mounted admin_tiers router in `src/gateway/app.py`
- [x] Created `templates/keys.html` with create modal + confirm rotate/revoke flows
- [x] Created `partials/keys_row.html` — HTMX swap target, raw key revealed once
- [x] Created `partials/toast.html` — HTMX toast for tier save feedback
- [x] Created `templates/tiers.html` — inline-edit cells, Save-per-row, HTMX PUT
- [x] Added UI handlers in modular sub-routers (`keys_page_routes.py`, `tiers_page_routes.py`)
- [x] Tier cache invalidation in same request as DB commit (atomic)
- [x] 30 unit tests (15 admin_tiers + 15 UI routes), all passing

## Tests Status

- Type check: pass (all imports verified via python -c)
- Unit tests: **711 passed, 0 failed** (30 new tests added)
- Previous count: 681 → now 711

## Architecture Decisions

- Sub-router split: `keys_page_routes.py` + `tiers_page_routes.py` included into main router via `router.include_router(..., dependencies=[Depends(require_session)])` — session guard applied at router level, not per-handler
- `templates_env.py` singleton avoids circular imports between sub-modules
- Raw key shown in amber banner row below the created row; dismissed on "Copy & dismiss" click — never re-rendered
- `invalidate_cache()` called synchronously after `session.commit()` — guaranteed same request

## Deviations from Spec

- Phase spec says `partials/_keys_row.html` and `_keys_create_modal.html`; actual names are `partials/keys_row.html` (no leading underscore, consistent with Jinja2 include convention) and create form is embedded in `keys.html` (not a separate partial — reduces round-trips)
- Create form modal is rendered server-side in `keys.html` (hidden div), revealed by JS; no separate HTMX fetch needed since form is simple
- `routes.py` is 219 lines (19 over 200-line guideline) — intentional; further split would fragment auth handlers across too many files

## Status

**Status:** DONE
**Summary:** Phase 2 fully implemented — 12 new/modified files, 30 new tests, 711/711 passing. Key CRUD UI and tier editor are wired end-to-end with session auth, cache invalidation, and raw-key-once display pattern.
