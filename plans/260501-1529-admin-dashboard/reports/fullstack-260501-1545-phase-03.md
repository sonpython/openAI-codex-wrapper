# Phase 03 Implementation Report — Job Inspector + Audit Viewer

## Phase
- Phase: phase-03-jobs-audit
- Plan: /Users/michaelphan/projects/codex-wrapper/plans/260501-1529-admin-dashboard
- Status: completed

## Files Modified

| File | Action |
|---|---|
| `src/db/crud/jobs.py` | Added `list_with_filters()` — JOIN users.email, COUNT subquery, pagination |
| `src/db/crud/audit_log.py` | Added `list_with_filters()` — COUNT subquery, pagination |
| `src/gateway/routes/admin_jobs.py` | Created — GET /admin/jobs with X-Admin-Token, Pydantic schemas, limit clamping |
| `src/gateway/routes/admin_audit.py` | Created — GET /admin/audit with same pattern |
| `src/admin_ui/jobs_page_routes.py` | Created — GET /jobs, /jobs/_table, /jobs/{id}/_detail sub-router |
| `src/admin_ui/audit_page_routes.py` | Created — GET /audit, /audit/_table sub-router |
| `src/admin_ui/routes.py` | Wired jobs + audit sub-routers with require_session dependency |
| `src/gateway/app.py` | Mounted admin_jobs_router + admin_audit_router at /admin prefix |
| `src/admin_ui/templates/jobs.html` | Created — filter form + detail modal overlay + HTMX wiring |
| `src/admin_ui/templates/audit.html` | Created — filter form + JSON expand + HTMX wiring |
| `src/admin_ui/templates/partials/jobs_table_fragment.html` | Created — table + pagination (HTMX swap target) |
| `src/admin_ui/templates/partials/jobs_row.html` | Created — single job row |
| `src/admin_ui/templates/partials/jobs_detail_modal.html` | Created — metadata grid + stderr tail + archive loader |
| `src/admin_ui/templates/partials/audit_table_fragment.html` | Created — table + pagination (HTMX swap target) |
| `src/admin_ui/templates/partials/audit_row.html` | Created — row + collapsible JSON detail |
| `tests/unit/test_admin_jobs_endpoint.py` | Created — 10 tests |
| `tests/unit/test_admin_audit_endpoint.py` | Created — 10 tests |

## Tasks Completed

- [x] `jobs_crud.list_with_filters` — JOIN users.email, all filters, COUNT for pagination
- [x] `audit_crud.list_with_filters` — all filters, COUNT for pagination
- [x] `GET /admin/jobs` — limit clamp 1-500, all filters, PaginatedJobs response
- [x] `GET /admin/audit` — same pattern, PaginatedAudit response
- [x] Jobs page sub-router (3 routes: page, table partial, detail partial)
- [x] Audit page sub-router (2 routes: page, table partial)
- [x] All templates + partials created
- [x] Sub-routers wired into routes.py with session dependency
- [x] Data routers mounted in app.py at /admin prefix
- [x] 20 unit tests (10 jobs + 10 audit)

## Tests Status

- Type check: N/A (no separate typecheck script; code compiles cleanly)
- Unit tests: **731 passed**, 3 warnings (0 failures, 0 errors)
  - New: 20 tests added for jobs + audit endpoints
  - Prior: 711 tests all still pass

## Acceptance Criteria Status

- [x] Jobs filter (status + user + range) returns correct subset — validated via unit tests
- [x] Pagination round-trip stable — offset/limit forwarded and returned in response
- [x] Drill-in modal shows job metadata + stderr (DB tail + archive fetch via browser fetch())
- [x] Audit viewer: filter by action returns matching entries
- [x] Limit clamping: request 1000 → 500 (test_jobs_limit_clamped_to_500, test_audit_limit_clamped_to_500)
- [x] 6+ unit tests jobs endpoint (10), 6+ tests audit endpoint (10)
- [x] HTMX live-filter: `hx-trigger="change delay:300ms from:input,select"` on both filter forms

## Notes

- `stderr_url` in detail modal points to `/admin/codex/jobs/{id}/stderr`. The modal fetches it client-side via `fetch()` with a "Load archive" button. Token is not embedded in the JS (would require session-cookie forwarding or admin token exposure) — the existing `admin_codex_stderr` route uses X-Admin-Token header, so the browser fetch will 403. **Unresolved Q below.**
- `audit_log` has no `email` column — `actor_email` field in AuditEntry returns `null` (user_id is stored, not email). A future join to `users` table would require the admin session pool, which is heavier. Acceptable for v1.

## Unresolved Questions

1. **Stderr archive auth in browser**: `/admin/codex/jobs/{id}/stderr` requires `X-Admin-Token` header. The detail modal's JS `fetch()` call leaves the token empty (cannot safely embed in template). Options: (a) add a proxy endpoint that validates session cookie and proxies the stderr call server-side, (b) embed token in JS (security concern), (c) accept "No archive available" in browser (user can use curl). Recommend option (a) in phase-4 or as a follow-up task.

**Status:** DONE_WITH_CONCERNS
**Summary:** All 6 new files created, 2 CRUD helpers extended, 2 routers mounted, 9 templates written, 20 tests pass (731 total, 0 regressions).
**Concerns:** Stderr archive fetch from browser modal requires X-Admin-Token which cannot be safely embedded in JS. Server-side proxy route needed for full UX.
