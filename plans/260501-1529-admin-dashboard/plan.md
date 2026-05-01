---
title: "Admin Dashboard v1 (HTMX + FastAPI)"
status: pending
priority: P2
created: 2026-05-01
estimate: "4d"
source: brainstorm
brainstorm: plans/reports/brainstorm-260501-1529-admin-dashboard.md
---

# Admin Dashboard v1

## Goal

Web UI tại `/admin/ui/*` cho admin tasks: dashboard, API key CRUD, tier editor, job inspector, audit viewer, per-user usage. HTMX + FastAPI in-process, không add infra service mới.

## Approach

HTMX + Jinja2 + Tailwind/Chart.js CDN. ADMIN_TOKEN form login → HttpOnly Redis-backed cookie session. Reuse existing X-Admin-Token data endpoints + add 6 new admin data endpoints.

## Phases

| # | Title | Effort | Status | Depends |
|---|---|---|---|---|
| 1 | [Scaffold + Auth + Dashboard](phase-01-scaffold-auth-dashboard.md) | 1d | pending | — |
| 2 | [Key CRUD + Tier Editor](phase-02-keys-tiers.md) | 1d | pending | 1 |
| 3 | [Job Inspector + Audit Viewer](phase-03-jobs-audit.md) | 1d | pending | 1 |
| 4 | [Per-User Usage + Live Metrics + Polish](phase-04-usage-live-polish.md) | 1d | pending | 1, 2, 3 |

## Key Dependencies

- **Existing:** `src/gateway/routes/admin_api_keys.py`, `src/gateway/routes/admin_codex_stderr.py`, `src/db/crud/plans.py:invalidate_cache()`, `src/observability/metrics.py`, Redis client `src/redis_client.py`
- **External (CDN):** htmx.org 2.x, tailwindcss 3.x browser build, chart.js 4.x
- **NEW Python deps:** `jinja2` (FastAPI optional, install), `itsdangerous` (cookie signing) — both already common

## Constraints

- Gateway runs `--workers 1` → `invalidate_cache()` direct call sufficient
- NO DB schema migrations
- Single ADMIN_TOKEN (rotation via restart, documented)
- Cookie session: signed with secret derived from ADMIN_TOKEN → token rotation invalidates all sessions
- Dev compose has no Prometheus → fallback `/_internal/metrics` text-format parser

## Success Criteria

- Login flow works end-to-end (form → cookie → dashboard)
- All 7 pages render & work via HTMX (no full reloads except login/logout)
- Cache invalidation observable: edit Plan → next request reflects new limits
- 20+ unit tests new endpoints, 5+ integration tests UI flow
- HA EOC remains functional (no regression to public `/v1/*`)
- Docs updated: README mentions `/admin/ui`, operations-runbook has rotate procedure
