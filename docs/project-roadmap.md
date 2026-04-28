# Project Roadmap

**Project:** Codex CLI OpenAI-Compatible Wrapper  
**Current Status:** v1 INTERNAL ONLY (feature-complete, production-ready)  
**Next Release:** v1.1 (planned enhancements)

---

## v1: INTERNAL ONLY (Current — Locked)

**Status:** ✅ Complete (all 11 phases shipped)  
**Scope:** ChatGPT login auth, no external customers, internal team use only  
**Test Coverage:** 615 unit tests passing; ≥75% coverage on key modules

### Completed Features

| Feature | Status | Notes |
|---------|--------|-------|
| **OpenAI Endpoints** | ✅ | GET /v1/models, POST /v1/chat/completions (sync + SSE), POST /v1/responses (sync + SSE) |
| **Chat Completions** | ✅ | Full OpenAI SDK compatibility (Python + Node.js); SSE streaming with keepalive |
| **Responses API** | ✅ | 50+ event taxonomy; async event publisher to Redis pub/sub |
| **Codex Jobs** | ✅ | POST/GET/DELETE /v1/codex/jobs; ephemeral workspace per task; diff generation |
| **Job Streaming** | ✅ | GET /v1/codex/jobs/{id}/events (SSE); replay old + subscribe new |
| **Bearer Auth** | ✅ | API key management (create, rotate, revoke); argon2id hashing |
| **Multi-Tier Rate Limit** | ✅ | RPM, TPM, concurrent, monthly quotas; OpenAI-style headers; Lua scripts in Redis |
| **SSRF Guard** | ✅ | URL validation; private IP rejection; safe HTTP transport |
| **Workspace Isolation** | ✅ | Path validation (realpath + commonpath); ephemeral cleanup; C6 red-team fix |
| **Sandbox Enforcement** | ✅ | Codex --sandbox workspace-write (Landlock/seccomp/Seatbelt) |
| **Observability Stack** | ✅ | structlog → Loki, Prometheus (16 instruments), OTEL → Tempo, Grafana dashboards |
| **Audit Log** | ✅ | Per-API-call logging; accessible via admin API |
| **Stderr Archive** | ✅ | Preserve subprocess stderr for debugging; indexed in DB |
| **Structured Logging** | ✅ | JSON logs with request_id propagation; secret redaction |
| **Backup & Disaster Recovery** | ✅ | Daily pg_dump + age encryption → S3; quarterly restore drill |
| **Admin API** | ✅ | POST /v1/admin/api-keys, PUT rotate, GET stderr archive |
| **Docker Compose Deploy** | ✅ | Single VM stack: gateway, worker, postgres, redis, caddy, otel, loki, tempo, prometheus, grafana |
| **Access Gate** | ✅ | Internal-only reachability (Cloudflare Access / Tailscale / IP allowlist TBD) |
| **SDK Compat Tests** | ✅ | Python + Node.js smoke tests; weekly real-codex drift cron |

### Metrics Achieved

- ✅ **615 unit tests** passing
- ✅ **~9,500 LOC** in src/ (81 modules)
- ✅ **p95 first-token latency** < 2s on /v1/chat/completions stream
- ✅ **Rate-limit accuracy** ±1% at 100 req/s
- ✅ **Zero cross-job workspace leak** (integration tests)
- ✅ **Zero secrets in logs** (CI grep gate)
- ✅ **Weekly drift detection** (real-codex cron enabled)
- ✅ **Access gate verified** (external port-scan returns zero open /v1/* ports)

---

## v1.1: Post-Launch Polish (Planned, Not on v1 Roadmap)

**Effort:** Medium (8-12 weeks)  
**Priority:** High (community feedback + operational maturity)  
**Scope:** Quality improvements + minor features

### Planned Enhancements

| Feature | Priority | Effort | Status | Notes |
|---------|----------|--------|--------|-------|
| **Tools / Function-Calling** | High | M | Planned | Synthesize OpenAI function-call syntax + responses. Codex CLI doesn't expose natively; requires wrapping. |
| **Multi-Account ChatGPT Pool** | High | L | Planned | Rotate between multiple ChatGPT login sessions (load balancing, resilience to single-account ban). Auth rotation via cron. |
| **GitHub PAT Support** | High | S | Planned | Accept GITHUB_TOKEN for private repo clone. Current: public repos only (422 on private). |
| **Webhook Callbacks for Jobs** | Medium | M | Planned | POST {callback_url} when job completes. Requires job state machine + webhook retry logic. |
| **TPM Window-Boundary Fairness** | Medium | S | Planned | Replace single-bucket TPM with two-bucket interpolation (defer from v1 if ≤1.5x burst observed). |
| **Stderr Archive → S3 Default** | Medium | S | Planned | Current: Postgres blob. Offload large stderr to S3; keep index in DB. |
| **Usage Reporting UI** | Low | M | Planned | Internal dashboard: monthly token usage, cost simulation, quota headroom. |
| **Job Result Webhook Signing** | Medium | S | Planned | HMAC-SHA256 signing for webhook payloads (security best practice). |
| **Auto-PR Generation** | Low | L | Deferred | Jobs→GitHub PR integration. Scope creep; defer post-v1. |

---

## v2: External Launch Path (Future)

**Effort:** Large (16-20 weeks)  
**Priority:** Strategic (scale to external users)  
**Scope:** Legal, billing, multi-tenant, public access

### Pre-Launch Requirements

| Requirement | Owner | Priority | Notes |
|-------------|-------|----------|-------|
| **Legal Review (ToS)** | Legal | CRITICAL | ChatGPT ToS violation assessment. Require switching to OPENAI_API_KEY mode before external launch. |
| **Billing Integration** | Product | CRITICAL | Map rate-limit quotas to paid plans. Meter usage. Invoice integration (Stripe / custom). |
| **Multi-Tenant Architecture** | Eng | CRITICAL | User isolation; per-user API key quotas; billing per user. Current: intra-team safety only. |
| **Public Access Gate Config** | Ops | CRITICAL | Remove internal-only access gate. TLS + public DNS. DDoS mitigation (Cloudflare). |
| **Compliance & Security Audit** | Security | HIGH | SOC 2 / ISO 27001 readiness. Penetration test. Data retention policy. |
| **Support Infrastructure** | Ops | HIGH | Runbook for SLAs. Escalation procedures. On-call rotation. |
| **Documentation** | Docs | HIGH | API docs (OpenAPI spec generation). Billing examples. Troubleshooting guide. |

### v2 Features

| Feature | Status | Notes |
|---------|--------|-------|
| **OPENAI_API_KEY Auth Mode** | Planned | Switch from ChatGPT login to direct OPENAI_API_KEY. Resolves ToS conflict. Code path exists in phase-02. |
| **Multi-Tenant Billing** | Planned | Per-user quotas, usage tracking, subscription tiers. |
| **Plan Management API** | Planned | Self-serve plan upgrade/downgrade. |
| **Billing Dashboard** | Planned | Customer-facing usage + invoices. |
| **Advanced Rate Limit** | Planned | Priority queues, burstable quotas, usage forecasting. |
| **Dedicated Account Option** | Planned | Single-tenant wrapper for enterprise customers. |
| **SLA & Support** | Planned | 99.5% uptime SLA. Priority support tiers. |

### Open Questions for v2

1. **Public access authentication** — Keep API key auth or layer OAuth2/SAML?
2. **Pricing model** — Per-token, monthly subscription, hybrid?
3. **Multi-cloud** — Stay on-premise or offer SaaS hosting?
4. **Integration** — Slack app, GitHub Actions, VS Code extension?

---

## Timeline & Phases (v1 Completed)

### v1 Implementation Phases (✅ All Done)

| Phase | Name | Status | Duration |
|-------|------|--------|----------|
| 0 | Bootstrap | ✅ | 1 week |
| 1 | Auth & Models | ✅ | 1 week |
| 2 | Codex Runner | ✅ | 2 weeks |
| 3 | Chat Completions | ✅ | 2 weeks |
| 4 | Responses API | ✅ | 2 weeks |
| 5 | Jobs & Arq | ✅ | 2 weeks |
| 6 | Rate-Limit Multi-Tier | ✅ | 2 weeks |
| 7 | Observability | ✅ | 2 weeks |
| 8 | Hardening | ✅ | 2 weeks |
| 9 | SDK Compat Tests | ✅ | 1 week |
| 10 | Deploy & Hardening | ✅ | 2 weeks |

**Total v1 effort:** ~8-10 weeks (actual: locked 2026-04-27)

---

## Success Metrics (v1 & Beyond)

### v1 Acceptance Criteria (✅ Met)

- [x] OpenAI Python + Node SDK pass smoke tests (sync + stream)
- [x] p95 latency < 2s on chat-completions stream
- [x] Zero cross-job workspace leak (integration tests)
- [x] Rate-limit accuracy ±1% at 100 req/s
- [x] Zero secrets in logs (CI grep gate)
- [x] Weekly real-codex drift cron green
- [x] Access gate enforces internal-only (external port-scan = 0 open ports)
- [x] 615 unit tests ≥75% coverage (key modules ≥85%)
- [x] Runbook reviewed + deployed

### v1.1 Goals (Estimated)

- SDK smoke tests expand to ≥5 languages (Python, Node, Go, Rust, etc.)
- p95 latency maintained < 2s even with function-calling overhead
- Multi-account ChatGPT pool failover < 30s
- Rate-limit TPM fairness improved to ±0.5% burst tolerance

### v2 Goals (Estimated)

- 99.5% uptime SLA (requires K8s + multi-region)
- < 500ms p95 latency (requires Codex optimization or caching)
- Support 10k+ external users
- $1M+ ARR (depends on pricing model)

---

## Open Items & Decisions Deferred

### Immediate (v1.1 Sprint 0)

| Item | Owner | Effort | Notes |
|------|-------|--------|-------|
| Access gate concrete pick | Ops | S | Choose: Cloudflare Access / Tailscale / IP+WireGuard allowlist. Org infra preference. |
| Internal domain name | Ops | S | E.g., `codex.internal` or subdomain. Add to Caddyfile. |
| ~/.codex/auth.json bootstrap | Ops | S | Document one-time `codex login --device-auth` flow for admin. |
| CODEX_AUTH_JSON_AGE setup | DevOps | S | GH Actions secret setup for age encryption key. Quarterly rotation schedule. |
| Real-codex cron tuning | Ops | S | Verify Sunday 03:00 UTC fits compat test window. Adjust if needed. |
| Slack / PagerDuty alert routing | Ops | S | Choose severity routing (critical → pages, warning → Slack). |

### Medium-term (v1.1)

| Item | Owner | Effort | Notes |
|------|-------|--------|-------|
| Tools/function-calling prototype | Eng | M | Synthesize OpenAI tool schema from prompts. Codex doesn't expose natively. |
| Multi-account ChatGPT pool PoC | Eng | M | Test account rotation + failover. Auth secret management (age + GH). |
| GitHub PAT support | Eng | S | Add GITHUB_TOKEN validation. Test private repo clone. |
| TPM two-bucket interpolation | Eng | S | If v1 load test shows > 1.5x TPM burst fairness, implement. |

### Strategic (v2)

| Item | Owner | Priority | Notes |
|------|-------|----------|-------|
| Legal: ChatGPT ToS review | Legal | CRITICAL | Assess external customer launch feasibility. Likely requires v2 OPENAI_API_KEY switch. |
| Billing system design | Product | CRITICAL | Choose: Stripe, custom, hybrid. Rate-limit → plan mapping. Metering logic. |
| Multi-tenant architecture design | Eng | CRITICAL | Audit isolation; quota enforcement; billing boundaries. High-risk feature. |
| K8s migration plan | Ops | HIGH | Scale beyond single VM. Multi-region for HA. Requires major refactor. |

---

## Risk Mitigation (Rolling Forward)

| Risk | Mitigation | Trigger | Owner |
|------|-----------|---------|-------|
| ChatGPT account ban (HIGH) | v1 INTERNAL ONLY scope. Monitor readiness probe. v2: switch to OPENAI_API_KEY. | Ban detected → alert escalation | Ops |
| Codex JSONL schema drift (MEDIUM) | Weekly real-codex cron. Pin @openai/codex@0.125.0 exactly. | Cron failure → auto-file GH issue | CI/Ops |
| Single-VM failure (MEDIUM) | Daily backup. Quarterly restore drill. | Restore failure → post-mortem | Ops |
| TPM fairness burst (MEDIUM) | v1: accept as known limit. v1.1: implement two-bucket interpolation. | Observed burst > 1.5x → backlog item | Eng |
| Legal ToS conflict (CRITICAL) | v1: internal-only. v2: OPENAI_API_KEY. Legal review required before external launch. | Legal objection → escalate | Legal |

---

## Release Cadence

### v1 (Current)

- **Status:** Locked. No planned changes except critical bug fixes.
- **Patch releases:** v1.0.1, v1.0.2, ... (security/hotfixes only)
- **Support:** Indefinite (internal tool; no deprecation timeline)

### v1.1

- **Target:** Q3 2026 (6-8 months post-v1 launch)
- **Freeze date:** TBD (depends on feedback, operational maturity)
- **Features:** Tools/function-calling, multi-account pool, GitHub PAT, webhook callbacks
- **Effort:** 8-12 weeks (if greenlit)

### v2

- **Target:** Q4 2026 or Q1 2027 (depends on legal + strategic decisions)
- **Pre-launch:** Legal review, billing system, multi-tenant audit, security audit
- **Effort:** 16-20 weeks (major refactor + new infrastructure)
- **Blocker:** Legal clearance on ChatGPT ToS + decision to switch to OPENAI_API_KEY mode

---

## Dependencies & Constraints

### External Dependencies (v1 Locked)

- `@openai/codex@0.125.0` (exact version; JSONL schema stability)
- OpenAI ChatGPT login (browser-based auth; toS risk)
- Postgres 16 (durable state)
- Redis 7 (queue + cache)
- Linux kernel ≥ 5.13 (Landlock for sandbox; macOS Seatbelt fallback)

### Internal Dependencies (v1 Locked)

- Single VM (< 1k users; vertical scale only)
- Docker Compose orchestration
- Internal access gate (Cloudflare / Tailscale / IP allowlist)

### v1.1 Dependencies

- Additional ChatGPT accounts (for multi-account pool) — auth mgmt overhead
- GitHub API tokens (for private repo support) — scope creep (SSRF risk)

### v2 Dependencies

- K8s cluster (for HA + multi-region)
- Billing platform (Stripe / custom)
- Legal clearance (ChatGPT ToS, OPENAI_API_KEY ToS)
- Security audit firm (SOC 2 / penetration test)

---

## Documentation Roadmap

### v1 Docs (✅ Complete)

- [x] README.md (quick start, features, structure)
- [x] project-overview-pdr.md (vision, scope, risks, metrics)
- [x] code-standards.md (file size, naming, async, testing)
- [x] codebase-summary.md (module tree, entry points, stats)
- [x] system-architecture.md (data flow, storage, rate-limit, observability)
- [x] project-roadmap.md (this file)
- [x] deployment-guide.md (VM sizing, first deploy, SSL setup)
- [x] operations-runbook.md (10 ops, 4 error codes, troubleshooting)
- [x] host-hardening.md (UFW, userns-remap, SSH, fail2ban)

### v1.1 Docs (Planned)

- [ ] API reference (OpenAPI spec auto-generated from FastAPI)
- [ ] Troubleshooting guide expansion (function-calling, multi-account failover)
- [ ] Multi-account pool runbook
- [ ] GitHub PAT setup guide

### v2 Docs (Planned)

- [ ] Billing guide (pricing, quotas, invoicing)
- [ ] Multi-tenant architecture documentation
- [ ] K8s deployment guide
- [ ] SLA / support policy documentation
- [ ] Compliance documentation (SOC 2, data retention)

---

## Related Links

- **[Implementation Plan](../plans/260427-1358-codex-openai-wrapper/plan.md)** — 11 completed phases with detailed specs
- **[Brainstorm Report](../plans/reports/brainstorm-260427-1358-codex-openai-wrapper.md)** — Architecture deep dive + 7 risks
- **[Open Questions Resolved](../plans/reports/brainstorm-260427-1727-open-questions-resolved.md)** — Final scope decisions (v1 INTERNAL ONLY, Loki/Tempo/age, drift cron)
- **[Code Review Reports](../plans/reports/)** — 7 code review cycles (red-team fixes applied)
- **[Tester Validation Reports](../plans/reports/)** — Unit test validation per phase

---

**Last Updated:** 2026-04-27  
**Next Review:** Post-v1 launch (feedback-driven)
