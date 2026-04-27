---
title: "Codex CLI OpenAI-Compatible API Wrapper (Internal v1)"
description: "Internal wrapper around `codex exec --json` exposing OpenAI-compatible HTTP API for SDK clients. INTERNAL/DEV use only — not for external paying customers under v1 ChatGPT-login mode."
status: pending
priority: P1
effort: 11 phases (~6-8 weeks)
created: 2026-04-27
last_updated: 2026-04-27
plan_dir: plans/260427-1358-codex-openai-wrapper
blockedBy: []
blocks: []
tags: [codex, openai-compat, fastapi, internal-only, single-tenant]
---

# Codex CLI OpenAI-Compatible Wrapper (INTERNAL v1)

> **Scope locked 2026-04-27:** v1 ships as INTERNAL/DEV tool only. No external paying customers. ChatGPT-login auth retained indefinitely. External launch (and switch to `OPENAI_API_KEY`) is a future v2 decision, not on the v1 roadmap. Multi-tier rate limit + admin API kept as defense-in-depth for intra-team safety, NOT as SaaS billing primitives.

## Problem
Wrap Codex CLI (`@openai/codex@0.125.0` via `codex exec --json`) behind an OpenAI-compatible HTTP gateway accessible only by trusted internal users. Any OpenAI SDK pointed at `base_url=https://wrapper.internal/v1` works for chat completions (sync+stream), responses API, models, plus a Codex-specific `/v1/codex/jobs` endpoint for repo-task workflows. Production-grade hardening for internal multi-user use: bearer auth, rate limit (intra-team safety), sandbox isolation, observability.

## Locked Decisions
See brainstorm `../reports/brainstorm-260427-1358-codex-openai-wrapper.md` §2 + §11 for full rationale. Key:

| Area | Decision |
|---|---|
| Stack | Python 3.12 + FastAPI + uvicorn[standard] + Arq |
| Endpoints | `/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/codex/jobs[*]` |
| Codex auth | ChatGPT login (`~/.codex/auth.json`), single account, INTERNAL ONLY |
| Wrapper auth | Bearer API key, argon2id hash, prefix shown once |
| Access gate | Internal-only (Cloudflare Access / Tailscale / IP allowlist) — no public Internet exposure |
| Sandbox | `--sandbox workspace-write` + Landlock/seccomp/Seatbelt; per-job ephemeral `/workspaces/{job_id}` |
| Storage | Postgres 16 (durable) + Redis 7 (queue + rate limit) |
| Streaming | SSE; chat-completions data-only; responses API event+data with full taxonomy |
| Rate limit | RPM + TPM + concurrent + monthly (intra-team safety, not billing); OpenAI-style `X-RateLimit-*` headers |
| Cancellation | `DELETE /v1/codex/jobs/{id}` → SIGTERM → 5s grace → SIGKILL |
| GitHub clone | Public repos only v1 (private rejected 422) |
| Deploy | Docker Compose + Caddy 2 (internal CA TLS) on single VM behind access gate |
| Observability | structlog → Loki, Prometheus, OpenTelemetry → Tempo |
| Backup | `pg_dump | age | s3` (age = modern key mgmt) |
| Drift defense | Weekly GH Actions cron `compat-real-codex.yml` runs real `@openai/codex@latest` against canned fixtures |

## Phases

| # | File | Status | Priority | Effort |
|---|---|---|---|---|
| 0 | [phase-00-bootstrap.md](phase-00-bootstrap.md) | pending | critical | S |
| 1 | [phase-01-auth-and-models.md](phase-01-auth-and-models.md) | pending | critical | S |
| 2 | [phase-02-codex-runner.md](phase-02-codex-runner.md) | pending | critical | M |
| 3 | [phase-03-chat-completions.md](phase-03-chat-completions.md) | pending | critical | M |
| 4 | [phase-04-responses-api.md](phase-04-responses-api.md) | pending | high | M |
| 5 | [phase-05-jobs-and-arq.md](phase-05-jobs-and-arq.md) | pending | high | L |
| 6 | [phase-06-rate-limit-multi-tier.md](phase-06-rate-limit-multi-tier.md) | pending | high | M |
| 7 | [phase-07-observability.md](phase-07-observability.md) | pending | high | M |
| 8 | [phase-08-hardening.md](phase-08-hardening.md) | pending | high | M |
| 9 | [phase-09-openai-sdk-compat-tests.md](phase-09-openai-sdk-compat-tests.md) | pending | critical | S |
| 10 | [phase-10-deploy-hardening.md](phase-10-deploy-hardening.md) | pending | high | M |

## Key Dependencies
- Codex CLI ≥ 0.125.0 (pinned exact: `@openai/codex@0.125.0`)
- ChatGPT login session (interactive bootstrap on host; `~/.codex` mounted RO into containers)
- Postgres 16 (durable state)
- Redis 7 (Arq queue + rate-limit sliding window)
- Caddy 2 (TLS reverse proxy + ACME)
- Linux host with kernel ≥ 5.13 (Landlock) for prod; macOS dev OK (Seatbelt)

## Success Metrics
- OpenAI Python + Node SDK pass smoke suite (chat sync, chat stream, responses, models)
- p95 first-token latency < 2s on `/v1/chat/completions` stream
- Best-effort internal availability (no public SLA — ChatGPT session refresh windows expected)
- Zero cross-job workspace leak (integration test asserts)
- Rate-limit accuracy ±1% at 100 req/s (intra-team safety bound)
- Zero secret leak in logs (CI grep gate)
- Weekly real-codex smoke job green (drift detection)

## Acceptance
All 11 phases marked completed. Phase 9 SDK smoke tests pass against deployed gateway. Phase 10 internal access gate (Cloudflare Access / Tailscale / IP allowlist) verified — wrapper NOT reachable from public Internet. Runbook reviewed.

## Risks (top)

### ChatGPT account ban / ToS conflict (HIGH, mitigated by INTERNAL ONLY scope)
Codex CLI ChatGPT auth resold via this API may violate OpenAI ChatGPT ToS. By limiting v1 to **internal use only** (no external paying customers, no public access), the resale-ToS-violation surface is eliminated. Internal personal/employee use of ChatGPT account on personal/team workflows is generally within ToS. Account ban risk drops from CRITICAL to HIGH (still possible if pattern looks anomalous to OpenAI).

**Mitigations:**
- (a) **Hard scope: INTERNAL ONLY** — phase-10 access gate enforces non-public reachability; no marketing/sign-up flow exists.
- (b) ChatGPT-login session refresh handled per phase-08 healthcheck + readiness probe.
- (c) Future external launch is a v2 decision — would require switching to `OPENAI_API_KEY` (phase-02 supports both code paths today).
- (d) No SLA promised; internal users tolerate refresh windows.

### Other top risks
- **Codex JSONL schema drift** (MED) → pin `@openai/codex@0.125.0` exact + phase-00 flag-availability gate + **weekly real-codex GH Actions cron** (phase-09) catches schema break ~7 days max delay.
- **ChatGPT session refresh requires browser interaction** (HIGH) → runbook documents `codex login --device-auth` flow; readiness probe sheds load via `/v1/codex/health`.
- **Single-VM deploy** (MED, accepted) → daily `pg_dump | age | s3` backup (phase-10) + restore drill quarterly. No HA; downtime tolerated for internal scope.

See brainstorm §7 for full matrix.
