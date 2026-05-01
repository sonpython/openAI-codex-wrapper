# Brainstorm — Admin Dashboard v1

**Date:** 2026-05-01
**Status:** APPROVED — ready for `/ck:plan`
**Scope:** v1 INTERNAL admin web UI for the codex-wrapper gateway.

---

## Problem statement

Gateway hiện chỉ có:
- `/v1/admin/api-keys` (CRUD JSON)
- `/v1/admin/codex/jobs/{id}/stderr`
- `/_internal/metrics` (Prometheus text)

Mỗi tác vụ admin (issue/revoke key, xem usage, debug job, tune tier) phải `curl` thủ công với `X-Admin-Token`. Không có time-series view, không có per-user breakdown, không có UI để tune Plan limits.

Mục tiêu: 1 trang admin tích hợp dashboard + key management + tier tuning + job inspector + audit viewer.

---

## Inventory hiện tại

### Endpoints

| Path | Method | Auth |
|---|---|---|
| `/v1/models` | GET | Bearer |
| `/v1/chat/completions` | POST | Bearer (sync + SSE) |
| `/v1/responses` | POST | Bearer (full OpenAI taxonomy) |
| `/v1/codex/jobs` | POST | Bearer |
| `/v1/codex/jobs/{id}` | GET | Bearer |
| `/v1/codex/jobs/{id}` | DELETE | Bearer |
| `/v1/codex/jobs/{id}/events` | GET | Bearer (SSE) |
| `/v1/admin/api-keys` | POST/GET | X-Admin-Token |
| `/v1/admin/api-keys/{id}/rotate` | POST | X-Admin-Token |
| `/v1/admin/api-keys/{id}` | DELETE | X-Admin-Token |
| `/v1/admin/codex/jobs/{id}/stderr` | GET | X-Admin-Token |
| `/_internal/metrics` | GET | internal |

### DB tables
`users` · `api_keys` (argon2id hash, tier) · `jobs` · `plans` (rpm/tpm/concurrent/monthly/tier) · `usage_counter` (composite PK user_id+period) · `audit_log`

### Observability
16 Prometheus instruments. structlog JSON. OTEL → Tempo. Loki via Promtail (production stack).

---

## Approaches evaluated

| Approach | Effort | UI | Maintain | Verdict |
|---|---|---|---|---|
| **A: HTMX + FastAPI in-process** | 4d | 6/10 | low | ✅ chosen |
| B: Next.js standalone | 5-7d | 9/10 | medium | overkill cho v1 INTERNAL |
| C: Grafana plugin | 3-5d | 8/10 chart, 4/10 forms | medium-high | forms cùi, Grafana plugin dev khó |

**Rationale chọn A:**
- v1 INTERNAL ≤ 2 admin → UI quality 6/10 đủ
- Zero new infra (mount route mới trong gateway)
- Single binary deploy
- Token efficiency cao (Jinja + HTMX < 10KB JS bundle)
- Future: nếu cần expand public self-service → migrate sang B sau

---

## Final design

### Tech stack
- FastAPI router `src/admin_ui/` mount tại `/admin/ui/*`
- Jinja2 templates
- HTMX (CDN) + Tailwind (CDN) + Chart.js (CDN)
- Auth: ADMIN_TOKEN form login → HttpOnly signed cookie `admin_session` (Redis-backed TTL 8h)

### Pages

| Path | Mục đích |
|---|---|
| `/admin/ui/login` | POST form, set cookie |
| `/admin/ui/` | 4 KPI cards (req rate, error rate, active jobs, queue depth) + 24h sparklines |
| `/admin/ui/keys` | Table list + Create modal + Rotate/Revoke |
| `/admin/ui/users` | Per-user list + nested keys + 30d usage chart |
| `/admin/ui/jobs` | Filter (status, user, range), table, drill-in stderr |
| `/admin/ui/audit` | Audit log table + filter (action, user, range) |
| `/admin/ui/tiers` | Plan editor (rpm/tpm/concurrent/monthly per tier) |

### New API endpoints

```
GET  /v1/admin/usage/summary?range=24h|7d|30d
GET  /v1/admin/usage/by-key/{key_id}?range=
GET  /v1/admin/users
GET  /v1/admin/users/{user_id}/keys
GET  /v1/admin/jobs?user_id=&status=&limit=&offset=
GET  /v1/admin/audit?action=&user_id=&limit=&offset=
GET  /v1/admin/tiers
PUT  /v1/admin/tiers/{tier}        # → invalidate_cache()
GET  /v1/admin/metrics/live        # curated Prometheus query proxy
```

Tất cả `X-Admin-Token` (UI middleware auto-inject từ cookie).

### File structure

```
src/admin_ui/
├── routes.py              # /admin/ui/* page handlers
├── auth.py                # cookie session (signed, Redis-backed)
├── prom_client.py         # Prometheus HTTP API helper
├── templates/{base,login,dashboard,keys,users,jobs,audit,tiers}.html
└── static/{app.js,style.css}

src/gateway/routes/
├── admin_usage.py         # NEW
├── admin_jobs.py          # NEW
├── admin_users.py         # NEW
├── admin_tiers.py         # NEW
├── admin_audit.py         # NEW
├── admin_metrics_live.py  # NEW
├── admin_api_keys.py      # exists
└── admin_codex_stderr.py  # exists
```

---

## Resolved open questions

### Q1 — Plan cache invalidation
- ✅ `src/db/crud/plans.py:94` exposes `invalidate_cache()`
- Gateway runs `--workers 1` → direct call đủ
- Future: nếu scale `--workers > 1` → add Redis pub/sub `cache:plan:invalidate`

### Q2 — Per-user usage source
| Use case | Source |
|---|---|
| Monthly KPI cards | `usage_counter` (PK lookup) |
| 30d daily chart | `jobs` (`GROUP BY date(created_at)`, đã có `ix_jobs_user_id`) |
| Live request rate | Prometheus `rate(http_requests_total[1m])` |
| Per-key breakdown | `jobs.api_key_id` |

Không migration. Future: nếu jobs > 100k/tháng → add composite index `(user_id, created_at)`.

### Q3 — ADMIN_TOKEN rotation
- Strategy: **restart-based** + helper script `scripts/rotate_admin_token.sh`
- ~5s downtime acceptable cho v1 INTERNAL
- Documented vào `docs/operations-runbook.md`
- Future v2: support multi-token grace window hoặc move admin → `api_keys` table với role

---

## Implementation considerations

### Risks & mitigations

| Risk | Mitigation |
|---|---|
| Dev compose không có Prometheus → live panels broken local | Fallback: scrape gateway `/_internal/metrics` text-format + parse 4-5 key counters |
| HTMX 401 trên expired session | Middleware: trên `/admin/ui/*` 401 + HX-Request header → return `HX-Redirect: /admin/ui/login` |
| Plan edit cache miss giữa requests | Atomic: `invalidate_cache()` ngay sau commit |
| ADMIN_TOKEN single-secret single-failure | Acceptable v1; future Phase 2 thêm OAuth + roles |
| Prometheus query khi prod stack chưa expose port | Add `prometheus:9090` Docker network nội bộ; gateway query trực tiếp |
| Cookie session secret rotation | Sign cookie với key derived từ ADMIN_TOKEN — token rotate → cookies invalidate |

### Dependencies (new)
- `jinja2` (dev dep, FastAPI tự có optional)
- `itsdangerous` (cookie signing) — có thể reuse `secrets.token_urlsafe`
- HTMX + Tailwind + Chart.js qua CDN, no npm

### Does NOT touch
- Existing public `/v1/*` routes
- DB schema (no migrations)
- Auth middleware cho `/v1/*`
- Codex runner / SSE stack

---

## Success criteria

- [ ] Login với ADMIN_TOKEN → cookie set → access dashboard
- [ ] Overview: 4 KPI cards + 24h sparklines (live data từ Prometheus hoặc fallback)
- [ ] Key CRUD end-to-end (issue → display raw key 1 lần → revoke → rotate)
- [ ] Tier editor: edit Plan row → middleware reflect new limits trong vòng 1 request (cache invalidated)
- [ ] Job inspector: filter (user/status/range) + drill-in stderr
- [ ] Audit log viewer với filter
- [ ] Per-user usage 30d daily chart
- [ ] Tests: 20+ unit cho new endpoints, 5+ integration cho UI flow (Playwright headless?)
- [ ] Docs updated: `docs/operations-runbook.md` (admin UI access), `README.md` (mention `/admin/ui`)

---

## Effort estimate

**Total: 4 ngày**

| Day | Deliverables |
|---|---|
| 1 | Scaffold `src/admin_ui/` + auth (cookie session) + base.html + login + dashboard skeleton + Prometheus client |
| 2 | Key CRUD UI + tier editor + cache invalidation |
| 3 | Job inspector + audit log viewer |
| 4 | Per-user usage stats + live Prometheus panels + polish + tests + docs |

---

## Next steps

1. Run `/ck:plan` để generate phased implementation plan
2. Plan → `/ck:do` execute từng phase
3. Test on local `docker compose up` → verify HA EOC vẫn work song song
4. Deploy bumped version

## Unresolved (defer)

- Cookie session storage: in-memory (single worker) vs Redis. Khuyến nghị Redis cho future-proof multi-worker, low cost (~30 LOC).
- Real-time push (WebSocket) cho live dashboard updates: defer; 5s polling là đủ.
- Audit log export CSV: defer to v2.
- Bulk operations (revoke many keys at once): defer to v2.
