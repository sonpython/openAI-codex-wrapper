---
phase: 4
title: "Per-User Usage + Live Metrics + Polish"
status: pending
priority: P2
effort: "1d"
dependencies: [1, 2, 3]
---

# Phase 4: Per-User Usage + Live Prometheus + Polish

## Overview

Per-user usage page (monthly KPI từ `usage_counter`, 30-day daily chart từ `jobs`). Live Prometheus panels cho dashboard (5s refresh). Add user list page. Final polish: tests, docs, scripts/rotate_admin_token.sh, runbook update.

## Requirements

- Functional:
  - `/admin/ui/users`: paginated user list (email, key_count, current_month_requests, current_month_tokens) + drill-in
  - `/admin/ui/users/{user_id}`: detail page với keys list, monthly KPIs, 30d daily chart (requests + tokens)
  - Live dashboard panels: poll `/admin/ui/_live` every 5s qua HTMX `hx-trigger="every 5s"` → return updated KPI fragment
  - Tier rate-limit gauge: visualize current usage vs tier limit (RPM/monthly_quota%)
- Non-functional:
  - Charts client-side via Chart.js
  - 5s polling acceptable (no WebSocket)
  - Per-user query bounded by limit/offset; no full table scan

## Architecture

```
[/admin/ui/users]
    │
    ├─ GET /v1/admin/users?limit=&offset=  (NEW)
    │   joined: users + COUNT(api_keys) + sum(usage_counter current month)
    │
    └─ Drill-in /admin/ui/users/{id}
       ├─ GET /v1/admin/users/{id}/keys (existing list, filtered)
       ├─ GET /v1/admin/usage/by-key/{id}?range=30d (NEW)
       │   FROM jobs WHERE api_key_id=? AND created_at >= now()-30d
       │   GROUP BY date(created_at)
       └─ Render Chart.js time-series

[/admin/ui/_live] HTMX poll
    └─ prom_client.query_curated() → return _kpi_cards.html partial
```

New endpoints:
- `GET /v1/admin/users?limit=&offset=` — list users với aggregates
- `GET /v1/admin/users/{user_id}/keys` — keys của user
- `GET /v1/admin/usage/summary?user_id=&range=24h|7d|30d` — daily series for chart
- `GET /v1/admin/usage/by-key/{key_id}?range=30d` — same per-key

## Related Code Files

- Create:
  - `src/gateway/routes/admin_users.py`
  - `src/gateway/routes/admin_usage.py`
  - `src/db/crud/users.py` — extend với `list_with_aggregates`
  - `src/admin_ui/templates/users.html`
  - `src/admin_ui/templates/user_detail.html`
  - `src/admin_ui/templates/_kpi_cards.html` — partial cho live polling
  - `scripts/rotate_admin_token.sh` — helper script
- Modify:
  - `src/admin_ui/routes.py` — add `/admin/ui/users`, `/admin/ui/users/{id}`, `/admin/ui/_live`
  - `src/admin_ui/templates/dashboard.html` — wire live polling
  - `src/gateway/app.py` — include new routers
  - `docs/operations-runbook.md` — admin UI access + rotate procedure
  - `README.md` — mention `/admin/ui` access

## Implementation Steps

1. CRUD `src/db/crud/users.py`:
   - `list_with_aggregates(session, limit, offset)` — JOIN `users` LEFT JOIN `api_keys` GROUP BY users.id; sub-select sum from `usage_counter` WHERE period = current month
2. Endpoint `admin_users.py`:
   - `GET /v1/admin/users` paginated with aggregates
   - `GET /v1/admin/users/{user_id}/keys` filtered list
3. Endpoint `admin_usage.py`:
   - `GET /v1/admin/usage/summary?user_id=&range=` — query `jobs` `GROUP BY date_trunc('day', created_at)`; range parser (24h, 7d, 30d)
   - `GET /v1/admin/usage/by-key/{key_id}?range=` — same filtered by api_key_id
4. Live endpoint `/admin/ui/_live`:
   - Returns `_kpi_cards.html` partial với 4 KPIs from prom_client.query_curated()
   - Cache 5s server-side để tránh hammering Prometheus
5. Chart.js setup in user_detail.html: line chart (requests blue, output_tokens orange)
6. Polish:
   - Tailwind precompile to `style.css` (optional, if CDN flagged as risk) — `npx tailwindcss -i base.css -o src/admin_ui/static/style.css --minify`
   - Add favicon, loading spinners (HTMX `htmx:beforeRequest`)
   - Toast component cho success/error
7. `scripts/rotate_admin_token.sh`:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   NEW=$(openssl rand -hex 32)
   sed -i.bak "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=$NEW/" .env
   docker compose up -d --no-deps gateway
   echo "New ADMIN_TOKEN: $NEW"
   echo "Old token in .env.bak (delete after stash)"
   ```
8. Docs:
   - `docs/operations-runbook.md`: section "Admin UI Access" (login URL, token rotate, session TTL)
   - `README.md`: add line "Admin UI: http://localhost:8000/admin/ui (login with ADMIN_TOKEN)"
9. Tests:
   - Unit: usage range parser, prom_client query_curated, users.list_with_aggregates
   - Integration: per-user 30d chart returns correct daily counts
   - E2E (optional): playwright headless walks login → dashboard → users → user detail

## Success Criteria

- [ ] Users list shows current-month aggregates correctly
- [ ] User detail 30d chart renders with real data from `jobs` table
- [ ] Live dashboard updates every 5s without flicker (HTMX `hx-swap="innerHTML transition:true"`)
- [ ] `scripts/rotate_admin_token.sh` rotates + restarts gateway end-to-end
- [ ] Operations runbook updated với rotate procedure
- [ ] README mentions `/admin/ui`
- [ ] Total tests across all phases: 30+ unit, 8+ integration
- [ ] No regression: HA EOC chat completion + tool calls still work
- [ ] All 7 pages accessible from navbar và HTMX-navigable

## Risk Assessment

| Risk | Mitigation |
|---|---|
| 30d aggregate query slow with high job volume | Composite index migration `(api_key_id, created_at)` if EXPLAIN shows seq scan |
| Live polling adds load to Prometheus | Server-side cache 5s; if Prometheus down, fallback returns last cached value với staleness banner |
| Tailwind CDN unavailable in air-gapped deploy | Phase 4 step 6: precompile to local `style.css` |
| Chart.js bundle size on every page load | Acceptable: cached by browser; future could async-load only on detail pages |
| Users list aggregate query has N+1 risk | Single JOIN + GROUP BY query; verified via EXPLAIN ANALYZE |
| Rotate script breaks on macOS sed -i (vs GNU) | Script uses `sed -i.bak` portable form |
