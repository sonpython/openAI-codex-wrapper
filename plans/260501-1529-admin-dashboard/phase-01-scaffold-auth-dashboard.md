---
phase: 1
title: "Scaffold + Auth + Dashboard"
status: pending
priority: P2
effort: "1d"
dependencies: []
---

# Phase 1: Scaffold + Auth + Dashboard

## Overview

Tạo `src/admin_ui/` package, mount routes vào FastAPI app, cookie session auth (Redis-backed), base template với HTMX/Tailwind/Chart.js CDN, login page, dashboard skeleton (4 KPI cards + 24h sparklines).

## Requirements

- Functional:
  - GET `/admin/ui/login` render form
  - POST `/admin/ui/login` validate ADMIN_TOKEN → set HttpOnly signed cookie `admin_session` → 302 dashboard
  - GET `/admin/ui/logout` clear cookie + redirect login
  - GET `/admin/ui/` dashboard với 4 KPI cards + sparkline charts (live data)
  - Middleware: `/admin/ui/*` (trừ login) yêu cầu valid session cookie; nếu HTMX request thiếu session → return `HX-Redirect: /admin/ui/login` header
- Non-functional:
  - Cookie signed với secret derived từ ADMIN_TOKEN (HMAC-SHA256)
  - Session TTL 8h, stored Redis key `admin_session:{sid}`
  - Constant-time token compare (`secrets.compare_digest`)
  - All static assets via CDN (no npm)

## Architecture

```
Request /admin/ui/*
    │
    ▼
[admin_ui_session_middleware]
    │  ├─ login path? skip
    │  ├─ has cookie? validate sig + Redis lookup → ok
    │  └─ no/invalid → 302 login (or HX-Redirect)
    ▼
[FastAPI route handler]
    ├─ Render Jinja2 template
    ├─ Or fetch data from /v1/admin/* via httpx (server-side, X-Admin-Token from settings)
    └─ Or query Prometheus directly (prom_client.py)
```

Prometheus client fallback: nếu `PROMETHEUS_URL` env unset → parse `/_internal/metrics` text-format locally.

## Related Code Files

- Create:
  - `src/admin_ui/__init__.py`
  - `src/admin_ui/routes.py` — page handlers
  - `src/admin_ui/auth.py` — cookie session (sign, verify, Redis store)
  - `src/admin_ui/prom_client.py` — Prometheus query helper + text-parser fallback
  - `src/admin_ui/templates/base.html` — layout, navbar, HTMX/Tailwind/Chart.js CDN includes
  - `src/admin_ui/templates/login.html`
  - `src/admin_ui/templates/dashboard.html`
  - `src/admin_ui/static/app.js` — minimal HTMX config (CSRF, error handling)
  - `src/admin_ui/static/style.css` — overrides (if any)
- Modify:
  - `src/gateway/app.py` — mount `admin_ui` router + StaticFiles + Jinja2 setup
  - `src/settings.py` — add `prometheus_url: str | None = None`, `admin_session_ttl_seconds: int = 28800`

## Implementation Steps

1. Add deps: `uv add jinja2 itsdangerous` (verify itsdangerous needed; can use stdlib `hmac` + `secrets` instead — prefer stdlib for KISS).
2. Create `src/admin_ui/auth.py`:
   - `sign_session(sid: str, secret: str) -> str` HMAC-SHA256
   - `verify_session(cookie_value: str, secret: str) -> str | None`
   - `create_session(redis, admin_token_hash) -> sid` (random 32-byte urlsafe)
   - `validate_session(redis, sid) -> bool`
   - `delete_session(redis, sid)`
3. Create `src/admin_ui/prom_client.py`:
   - If `settings.prometheus_url`: query `/api/v1/query` and `/api/v1/query_range`
   - Else: fetch `localhost:8000/_internal/metrics`, parse text format, return last value
   - Curated queries: `req_rate_1m`, `error_rate_5m`, `active_jobs`, `queue_depth`, `req_24h_series`, `error_24h_series`
4. Create `src/admin_ui/routes.py`:
   - APIRouter prefix=`/admin/ui`
   - Routes: GET `/login`, POST `/login`, GET `/logout`, GET `/` (dashboard)
   - Dependency `require_admin_session` cho mọi route trừ login
5. Create templates:
   - `base.html`: HTML5, Tailwind CDN, HTMX 2.x CDN, Chart.js 4.x CDN, navbar với links 7 pages, body block
   - `login.html`: form POST `/admin/ui/login` với password field
   - `dashboard.html`: 4 KPI cards (grid), 2 sparkline canvases, refresh button (HTMX poll every 5s)
6. Mount router + StaticFiles trong `src/gateway/app.py`:
   ```python
   from src.admin_ui.routes import router as admin_ui_router
   from fastapi.staticfiles import StaticFiles
   app.include_router(admin_ui_router)
   app.mount("/admin/ui/static", StaticFiles(directory="src/admin_ui/static"))
   ```
7. Smoke test: `curl -X POST localhost:8000/admin/ui/login -d "token=$ADMIN_TOKEN"` → check `Set-Cookie: admin_session=...`; follow redirect to `/admin/ui/` → 200 with HTML.

## Success Criteria

- [ ] Login form renders + accepts valid token + rejects invalid (timing-safe)
- [ ] Cookie signed + verified + Redis-stored với TTL 8h
- [ ] Logout clears cookie + Redis entry
- [ ] HTMX request without session → `HX-Redirect: /admin/ui/login`
- [ ] Dashboard loads với 4 KPI cards, sparklines render Chart.js
- [ ] Prometheus fallback works locally (parse `/_internal/metrics` khi `PROMETHEUS_URL` unset)
- [ ] 8+ unit tests: auth helpers, route handlers, prom_client parsing
- [ ] Browser test: login → dashboard → logout flow

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Tailwind CDN slow / blocked | Document offline option (precompile `style.css` qua Tailwind CLI Phase 4) |
| Prometheus text parsing fragile | Test với golden fixture; prefer official `prometheus_client` if available |
| Cookie sig mismatch on ADMIN_TOKEN rotate | Documented behavior — all sessions invalidated, admin re-login |
| Session leak via XSS | Mitigated: HttpOnly + SameSite=Strict; templates auto-escape Jinja2 |
| Static files not served from Docker | Verify Dockerfile copies `src/admin_ui/static/` |
