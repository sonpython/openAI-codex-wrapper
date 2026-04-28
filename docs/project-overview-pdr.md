# Project Overview & PDR: Codex CLI OpenAI-Compatible Wrapper (Internal v1)

## Vision

Deliver a production-grade HTTP API wrapper exposing OpenAI-compatible endpoints around Codex CLI's `codex exec --json` subprocess, enabling internal teams to use OpenAI SDKs (Python, Node.js, etc.) against Codex with enterprise hardening (auth, rate-limit, observability, sandbox isolation).

## Scope: v1 INTERNAL ONLY

**v1 ships exclusively for internal team use.** No external paying customers. ChatGPT login auth model retained indefinitely for v1. External launch (switching to `OPENAI_API_KEY` mode) is a future v2 decision, explicitly deferred from v1 scope.

**Rationale:** ChatGPT login resold via this API may violate OpenAI's ToS if offered to external paying customers. By limiting v1 to internal (trusted employees/contractors), the resale-conflict risk is eliminated. Multi-tier rate limit and admin API, included as defense-in-depth for intra-team safety, are NOT positioned as SaaS billing primitives.

## Problem Statement

Teams using Codex CLI today must:
1. Wrap `codex exec --json` subprocess calls manually
2. Implement their own rate-limit + auth + observability
3. Deal with security/isolation concerns across multiple concurrent tasks
4. Lack OpenAI SDK compatibility (Python/Node clients can't target Codex directly)

**Solution:** Expose Codex as a drop-in replacement for OpenAI's API via `base_url` parameter. SDKs already know how to talk to it.

## Target Users

- Internal engineering teams using Codex for code review, refactoring, bug fixes
- Agentic workflows needing synchronous + streaming chat endpoints
- Task-based jobs (repo changes, diffs) via Codex-specific `/v1/codex/jobs` API
- Internal tools / scripts already leveraging OpenAI SDK patterns

## Key Features

### Core OpenAI Endpoints
- `GET /v1/models` — List available models (returns `{"data": [{"id": "codex"}]}`)
- `POST /v1/chat/completions` — Chat sync + SSE streaming
- `POST /v1/responses` — Responses API with 50+ event taxonomy (sync + SSE)

### Codex-Specific
- `POST /v1/codex/jobs` — Enqueue task: clone repo → run Codex → generate diff
- `GET /v1/codex/jobs/{id}` — Poll job status + result summary
- `DELETE /v1/codex/jobs/{id}` — Cancel running job (SIGTERM → 5s grace → SIGKILL)
- `GET /v1/codex/jobs/{id}/events` — Stream job lifecycle events (SSE)

### Multi-Tier Rate Limiting
- **RPM (requests/minute)** per API key (sliding window, Lua script in Redis)
- **TPM (tokens/minute)** per API key (counter with refresh-on-window-slide)
- **Concurrent requests** per user (PEXPIRE-refreshed counter in Redis)
- **Monthly quota** per plan tier (Postgres counter + Redis cache)
- OpenAI-compatible `X-RateLimit-*` response headers

### Authentication & Authorization
- Bearer token auth (API key lookup in Postgres)
- argon2id hashing (no plaintext storage)
- API key prefix shown once at creation (UX pattern from cloud platforms)
- Per-user audit log (all API calls recorded to `audit_log` table)
- Admin API for key rotation + management

### Production Observability
- **Logging:** structlog JSON (request_id, service, level, event, ts) → Promtail → Loki
- **Metrics:** 16 Prometheus instruments (latency p50/p95/p99, error counts, queue depth, rate-limit headroom, job duration, workspace usage)
- **Tracing:** OpenTelemetry OTLP → Tempo (distributed tracing across gateway + workers)
- **Alerting:** Prometheus alertmanager rules for critical paths (auth failures, queue backlog, workspace errors)

### Hardening
- **SSRF guard:** URL validation + transport override (no raw requests to untrusted hosts)
- **Workspace isolation:** `os.path.realpath` + `os.path.commonpath` check (prevents `../` escape)
- **Sandbox enforcement:** Codex `--sandbox workspace-write` (Landlock on Linux, seccomp, Seatbelt on macOS)
- **Timeout middleware:** Per-request timeout configurable, SSE connections exempt from hard cutoff
- **Stderr archive:** Preserve subprocess stderr for troubleshooting (indexed in Postgres, optional S3 backup)
- **Request ID propagation:** Unique per request, threaded through all logs/traces

## Locked Decisions (v1)

| Area | Decision | Rationale |
|------|----------|-----------|
| **Stack** | Python 3.12 + FastAPI + uvicorn[standard] + Arq | Async fits subprocess streaming; fast dev velocity; Arq = asyncio-native (lighter than Celery). |
| **Codex Auth** | ChatGPT login (`~/.codex/auth.json`), single account | User priority: avoid OpenAI API token cost. Session refreshable via readiness probe healthcheck. |
| **Wrapper Auth** | Bearer API key (argon2id hash) | Standard SaaS pattern. Prefix shown once (UX best practice). Per-key audit log. |
| **Access Gate** | Internal-only (Cloudflare Access / Tailscale / IP allowlist) | No public Internet exposure. Pick one per org infra. Phase-10 acceptance gate: external port-scan returns zero open `/v1/*` ports. |
| **Sandbox** | `--sandbox workspace-write` + Landlock/seccomp/Seatbelt | Built-in to Codex; lighter than Docker-per-job; sufficient for internal scope. |
| **Storage** | Postgres 16 (durable) + Redis 7 (queue, rate-limit, cache) | Postgres: users, api_keys, jobs, audit_log, usage_counter. Redis: rate-limit sliding window (Lua), Arq queue, pub/sub. |
| **Streaming** | Server-Sent Events (SSE) | Chat completions: data-only SSE. Responses API: full event taxonomy (50+ event types). |
| **Rate Limit** | RPM + TPM + concurrent + monthly | OpenAI-style headers. Intra-team safety, NOT billing (v1). Window-boundary unfairness in TPM deferred to v1.1. |
| **Cancellation** | DELETE → SIGTERM → 5s grace → SIGKILL | Quick stop without zombie cleanup burden. |
| **GitHub Clone** | Public repos only (v1; private rejected 422) | SSRF + auth overhead deferred. v1.1: add GitHub PAT support. |
| **Deployment** | Docker Compose + Caddy 2 (TLS) on single VM | Single-node start. VM behind access gate. Vertical scale sufficient for <1k internal users. |
| **Observability Stack** | structlog + Prometheus + OpenTelemetry → Loki + Tempo + Grafana | Self-hosted (fits Docker Compose). Grafana-native integrations. No AWS lock-in. Free. |
| **Backup** | `pg_dump | age | s3` | age = modern key mgmt; single pubkey recipient; no GnuPG keyring drama. |
| **Drift Defense** | Weekly GH Actions cron (real-codex smoke test) | Catches Codex JSONL schema breaks ~7 days max; auto-files GH issue on fail. Cheap insurance (~30min/week). |

## Out of Scope (v1)

- **Tools / function-calling** — Codex CLI doesn't expose; faking = brittle
- **Vision, multimodal** — Text-only scope
- **Fine-tuning, embeddings, audio** — Not in Codex CLI scope
- **Public / external customers** — v2 decision; would require legal/compliance review + OPENAI_API_KEY auth switch
- **Multi-account ChatGPT pool** — Single session (not auto-load-balanced)
- **Webhook callbacks for jobs** — v1.1 feature
- **Auto-PR generation** — v1.1 feature
- **Billing integration** — Rate limit primitives exist; monetization deferred

## Success Metrics

| Metric | Target | Validation |
|--------|--------|-----------|
| SDK compatibility | OpenAI Python + Node smoke tests pass | `compat-*-sdk.yml` workflows green; both SDK sync + stream paths work |
| p95 latency (first-token) | < 2s on `/v1/chat/completions` stream | Load test in phase-10; Tempo traces confirm |
| Internal availability | Best-effort (no public SLA) | ChatGPT refresh windows expected; readiness probe handles gracefully |
| Cross-job isolation | Zero workspace leak | Integration test asserts workspace content isolation per job_id |
| Rate-limit accuracy | ±1% at 100 req/s | Load test in phase-06; confirm RPM/TPM counters ±1% of expected |
| Secret safety | Zero secret leak in logs | CI `grep` gate on logs (searches for `sk-`, auth tokens, etc.) |
| Drift detection | Weekly real-codex smoke green | `compat-real-codex.yml` runs Sunday 03:00 UTC; auto-files GH issue on failure |
| Audit log coverage | 100% of API calls logged | Sample test: call each endpoint, verify audit_log has entries |

## Risks (Top 5)

### 1. ChatGPT Account Ban / ToS Conflict (HIGH, mitigated by INTERNAL ONLY)

**Risk:** Codex CLI ChatGPT auth resold via this API may violate OpenAI's ChatGPT ToS (clause on account resale/multi-party use).

**Mitigation:**
- **Hard scope: v1 INTERNAL ONLY** — No external paying customers, no public access, no sign-up flow
- Phase-10 access gate (Cloudflare Access / Tailscale / IP allowlist) enforces non-public reachability
- External launch = v2 decision; would require switching to `OPENAI_API_KEY` (phase-02 code path exists; legal review TBD)
- Internal use of personal/employee ChatGPT accounts on personal/team workflows generally within ToS
- No SLA promised; internal users tolerate refresh windows

**Residual Risk:** Account ban possible if usage pattern looks anomalous to OpenAI. Mitigation: monitor account health via readiness probe; alert on refresh failures.

### 2. Codex JSONL Schema Drift (MEDIUM)

**Risk:** Codex CLI future versions may change event JSONL schema, breaking event parsing.

**Mitigation:**
- Pin `@openai/codex@0.125.0` exactly in Dockerfile
- Phase-00 `make verify-codex` pre-flight gate asserts version + flag availability
- **Weekly real-codex smoke cron** (phase-09): runs actual `@openai/codex@latest` against canned fixtures; auto-files GH issue on schema break
- Max delay before detection: ~7 days

### 3. ChatGPT Session Refresh Requires Browser Interaction (HIGH)

**Risk:** ChatGPT login session expires; `codex login --device-auth` requires interactive browser flow.

**Mitigation:**
- Runbook documents refresh procedure
- `GET /readyz` healthcheck polls codex CLI; returns 503 if auth broken
- SSE endpoints use readiness probe to shed load gracefully
- No auto-recovery (human action required); acceptable for internal scope

### 4. Single-VM Deploy (MEDIUM, accepted)

**Risk:** No HA; single VM failure = total service outage.

**Mitigation:**
- Daily `pg_dump | age | s3` backup
- Restore drill quarterly
- Downtime tolerated for internal scope; SLA not promised
- Vertical scale sufficient for <1k users

### 5. Access Gate Misconfiguration → Public Exposure (HIGH)

**Risk:** Ops misconfigures Cloudflare Access / Tailscale / IP allowlist; wrapper becomes public.

**Mitigation:**
- Phase-10 acceptance criteria: external port-scan from third-party host returns ZERO open `/v1/*` ports
- Documented in runbook + deployment checklist
- CI gate: test from external IP confirms non-reachability

## Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| **Availability** | Best-effort for internal team; no public SLA |
| **Latency** | p95 first-token < 2s on streaming endpoints |
| **Throughput** | 100 concurrent requests (tuned in phase-06) |
| **Scalability** | Vertical scale on single VM sufficient; K8s migration future |
| **Data durability** | Daily backups; Postgres 16 replication future |
| **Security** | Bearer auth, argon2id, SSRF guard, workspace isolation, audit log |
| **Auditability** | 100% of API calls logged to audit_log; accessible via admin API |
| **Observability** | Logs (Loki), metrics (Prometheus), traces (Tempo) in single Grafana pane |
| **Reproducibility** | Dockerfile pinned deps; `uv sync` reproducible; lockfile committed |

## Implementation Phases

11 phases completed (all v1 features delivered):

| Phase | Name | Status |
|-------|------|--------|
| 0 | Bootstrap | ✓ Complete |
| 1 | Auth & Models | ✓ Complete |
| 2 | Codex Runner | ✓ Complete |
| 3 | Chat Completions | ✓ Complete |
| 4 | Responses API | ✓ Complete |
| 5 | Jobs & Arq | ✓ Complete |
| 6 | Rate-Limit Multi-Tier | ✓ Complete |
| 7 | Observability | ✓ Complete |
| 8 | Hardening | ✓ Complete |
| 9 | SDK Compat Tests | ✓ Complete |
| 10 | Deploy & Hardening | ✓ Complete |

See [implementation plan](../plans/260427-1358-codex-openai-wrapper/plan.md) for detailed phase specs.

## Version History

| Version | Date | Status | Notes |
|---------|------|--------|-------|
| v1 (INTERNAL ONLY) | 2026-04-27 | Locked | ChatGPT login auth, single account. No external customers. |
| v1.1 (planned) | TBD | Backlog | Tools/function-calling synth, multi-account pool, GitHub PAT, webhooks, better TPM fairness |
| v2 (planned) | TBD | Backlog | External launch path: OPENAI_API_KEY mode, public access gate, multi-tenant billing |

## Acceptance Criteria (v1 complete)

- [x] All 11 phases implemented and tested
- [x] OpenAI Python + Node SDK smoke tests pass (sync + stream)
- [x] p95 latency measured < 2s (phase-10 load test)
- [x] Zero cross-job workspace leak (integration tests)
- [x] Rate-limit accuracy ±1% at 100 req/s
- [x] Zero secrets in logs (CI grep gate)
- [x] Weekly real-codex cron green (drift detection)
- [x] Access gate enforces internal-only reachability (external port-scan returns zero open ports)
- [x] Runbook reviewed + deployed
- [x] 615 unit tests passing; 75%+ coverage on key modules

---

**Status:** v1 INTERNAL ONLY scope locked. Feature-complete. Production-ready.

**Last Updated:** 2026-04-27
