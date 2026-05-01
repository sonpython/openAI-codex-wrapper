# Phase 01 Implementation Report — Scaffold + Auth + Dashboard

## Status
DONE

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `src/admin_ui/__init__.py` | 1 | Package marker |
| `src/admin_ui/auth.py` | 95 | HMAC-SHA256 cookie signing, Redis session CRUD |
| `src/admin_ui/prom_client.py` | 165 | Prometheus query + local text-format fallback |
| `src/admin_ui/routes.py` | 175 | FastAPI router: login/logout/dashboard/partials |
| `src/admin_ui/templates/base.html` | 55 | Layout, Tailwind/HTMX/Chart.js CDN, navbar |
| `src/admin_ui/templates/login.html` | 45 | Login form, no base extend (standalone) |
| `src/admin_ui/templates/dashboard.html` | 70 | 4 KPI cards + 2 sparkline canvases, HTMX poll |
| `src/admin_ui/templates/partials/kpis.html` | 38 | HTMX KPI fragment (re-rendered every 5s) |
| `src/admin_ui/static/app.js` | 55 | HTMX error handler, toast helper |
| `src/admin_ui/static/style.css` | 18 | htmx-indicator + card hover overrides |
| `tests/unit/test_admin_ui_auth.py` | 95 | 14 auth helper tests |
| `tests/unit/test_admin_ui_prom_client.py` | 90 | 11 prom_client tests |
| `tests/unit/test_admin_ui_routes.py` | 200 | 9 route handler tests |

## Files Modified

| File | Change |
|------|--------|
| `src/settings.py` | Added `prometheus_url: str | None = None`, `admin_session_ttl_seconds: int = 28800` |
| `src/gateway/app.py` | Mounted `admin_ui_router`, `StaticFiles`, session-required exception handler |
| `pyproject.toml` | Added `jinja2>=3.1.6`, `python-multipart==0.0.27` (FastAPI Form dep) |

## Test Results

```
681 passed, 3 warnings in 7.49s
  └─ 647 pre-existing + 34 new (14 auth, 11 prom_client, 9 routes)
```

Route count: 5 (`/login` GET, `/login` POST, `/logout`, `/`, `/partials/kpis`)

## Acceptance Criteria Check

- [x] Login form renders + accepts valid token + rejects invalid (constant-time compare)
- [x] Cookie signed (HMAC-SHA256) + Redis-stored with TTL 8h
- [x] Logout clears cookie + Redis entry
- [x] HTMX request without session → `HX-Redirect: /admin/ui/login` (204 response)
- [x] Dashboard loads with 4 KPI cards + sparklines (Chart.js)
- [x] Prometheus fallback works — `parse_prometheus_text` parses `/_internal/metrics` text format
- [x] 34 unit tests (exceeds 8+ requirement)

## Deviations from Plan

- `python-multipart` added as dep (required by FastAPI for `Form()` — not anticipated in plan but correct)
- `itsdangerous` NOT added — stdlib `hmac` + `secrets` used per KISS constraint
- Exception handler for session redirect registered on `create_app()` (app-level, not router-level — FastAPI limitation)
- HTMX partial route (`/partials/kpis`) added beyond original 4-route spec for proper polling support

## Concerns

None blocking. The 3 warnings are httpx `DeprecationWarning` for per-request cookies in tests — cosmetic, does not affect correctness. Will disappear when `TestClient` API is updated in later pytest-httpx version.

## Docs Impact
minor — `pyproject.toml` updated with new deps.
