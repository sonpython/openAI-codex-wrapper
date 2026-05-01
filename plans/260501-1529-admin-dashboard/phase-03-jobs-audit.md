---
phase: 3
title: "Job Inspector + Audit Viewer"
status: pending
priority: P2
effort: "1d"
dependencies: [1]
---

# Phase 3: Job Inspector + Audit Log Viewer

## Overview

UI để debug failed jobs (filter, drill-in stderr) và xem audit log với filter. Add 2 read-only data endpoints. Reuse existing `/v1/admin/codex/jobs/{id}/stderr`.

## Requirements

- Functional:
  - `/admin/ui/jobs`: filter form (status, user_id, date range, limit) + paginated table (id, user_email, status, model, created_at, duration_ms, exit_code) + drill-in modal showing job detail + stderr archive
  - `/admin/ui/audit`: filter form (action, user_id, date range, limit) + paginated table (timestamp, actor, action, target, ip, status) + JSON detail expand
  - HTMX pagination (Next/Prev) replaces table body
- Non-functional:
  - Default limit 50, max 500
  - Audit log queries respect existing retention (90 days default)
  - PII redaction: prompt hashes only (existing behavior preserved)

## Architecture

```
[/admin/ui/jobs]            [/admin/ui/audit]
    │                           │
    ├─ GET /v1/admin/jobs       ├─ GET /v1/admin/audit
    │   filter+paginate          │  filter+paginate
    │                            │
    └─ Drill-in modal:           └─ JSON detail inline expand
       GET /v1/admin/codex/jobs/{id}/stderr  (existing)
```

New endpoints:
- `GET /v1/admin/jobs?user_id=&status=&from=&to=&limit=&offset=` — list jobs joined with users.email; index `ix_jobs_user_id` + `ix_jobs_status` cover most filters
- `GET /v1/admin/audit?action=&user_id=&from=&to=&limit=&offset=` — list audit entries

## Related Code Files

- Create:
  - `src/gateway/routes/admin_jobs.py`
  - `src/gateway/routes/admin_audit.py`
  - `src/db/crud/jobs.py` — extend with `list_with_filters(session, **filters)` if not present
  - `src/db/crud/audit_log.py` — extend with `list_with_filters` (or create file if not exist)
  - `src/admin_ui/templates/jobs.html`
  - `src/admin_ui/templates/_jobs_row.html`
  - `src/admin_ui/templates/_jobs_detail_modal.html`
  - `src/admin_ui/templates/audit.html`
  - `src/admin_ui/templates/_audit_row.html`
- Modify:
  - `src/admin_ui/routes.py` — add `/admin/ui/jobs`, `/admin/ui/audit` page handlers
  - `src/gateway/app.py` — include `admin_jobs`, `admin_audit` routers

## Implementation Steps

1. CRUD helpers:
   - `src/db/crud/jobs.py`: `async def list_with_filters(session, user_id=None, status=None, from_=None, to=None, limit=50, offset=0)` returning rows joined với `users.email`
   - `src/db/crud/audit_log.py`: similar
2. Pydantic response schemas: `JobSummary`, `AuditEntry`, `PaginatedJobs`, `PaginatedAudit`
3. `src/gateway/routes/admin_jobs.py`:
   - `GET /v1/admin/jobs` — clamp limit ≤ 500, return `{items, total, limit, offset}`
4. `src/gateway/routes/admin_audit.py` — same pattern
5. Page handlers:
   - GET `/admin/ui/jobs` → render filter form + initial page
   - GET `/admin/ui/jobs/_table` (HTMX target) → render `_jobs_row.html` partial loop
   - GET `/admin/ui/jobs/{id}/_detail` → fetch job + stderr archive → render `_jobs_detail_modal.html`
   - GET `/admin/ui/audit` + `/admin/ui/audit/_table` similar
6. Templates: filter form với HTMX `hx-get` `hx-target="#table"` `hx-trigger="change delay:300ms from:input,select"` cho live filter
7. Tests:
   - Unit: filter + pagination boundaries (offset 0, large offset, limit > 500 clamped)
   - Integration: create N jobs → query → verify pagination correct
   - UI: filter change triggers HTMX swap

## Success Criteria

- [ ] Jobs filter (status + user + range) returns correct subset
- [ ] Pagination: page 1 → page 2 → page 1 round-trip stable
- [ ] Drill-in modal shows job metadata + stderr archive (truncated to 1MB display)
- [ ] Audit viewer: filter by action returns matching entries
- [ ] Limit clamping enforced (request 1000 → 500 returned)
- [ ] 6+ unit tests jobs endpoint, 6+ tests audit endpoint
- [ ] HTMX live-filter works smoothly (300ms debounce)

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Audit log query slow without index on `(action, created_at)` | Add migration in future phase if needed; v1 use existing `created_at` desc + LIMIT |
| Job list slow with 100k+ rows | Composite index `(user_id, created_at)` future optimization (noted brainstorm Q2) |
| Stderr archive missing for old jobs | Existing endpoint handles 404; UI shows "No archive available" |
| User email join leaks PII to admin (intended) | Acceptable; admin role has full visibility per design |
| Filter SQL injection | Use ORM params; no raw SQL |
