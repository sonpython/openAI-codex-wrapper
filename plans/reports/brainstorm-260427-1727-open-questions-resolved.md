# Brainstorm Resolutions — 4 Open Questions (Round 3)

**Date:** 2026-04-27 17:27 GMT+7
**Continuation of:** `brainstorm-260427-1358-codex-openai-wrapper.md`
**Trigger:** Lead requested resolution of 4 questions left open after red team review.

---

## Decisions Locked

| # | Question | Decision | Rationale |
|---|---|---|---|
| Q1 | Launch mode (C7 ChatGPT ToS) | **v1 INTERNAL ONLY, ChatGPT login forever** | Scope removes ToS-resale conflict. No paying external users. External launch = v2 decision; would require switching to OPENAI_API_KEY (phase-02 already supports both). |
| Q2 | TPM window-boundary unfairness | **Defer to v1.1, document as known limit** | 2x burst window narrow; internal scope mitigates. Phase-10 load test will quantify; if observed > 1.5x sustained, swap to two-bucket interpolation v1.1. |
| Q3a | Logs: Loki vs CloudWatch | **Loki** | Self-host fits Docker-Compose deploy; Grafana-native; free; no AWS lock-in. |
| Q3b | Traces: Tempo vs Jaeger | **Tempo** | Grafana-native; OTLP first-class; same pane-of-glass with Prometheus + Loki. |
| Q3c | Backup encryption: age vs gpg | **age** | Modern key mgmt; single recipient pubkey; 2-line CLI; no GnuPG keyring drama. |
| Q4 | Real-codex weekly smoke job | **Add weekly cron in v1** | Mock-codex doesn't catch JSONL schema drift. Cheap (~30min CI/week + 1 ChatGPT request) insurance. Auto-files GH issue on fail. |

---

## Plan File Impact (applied)

| File | Change |
|---|---|
| `plan.md` | Re-titled "Internal v1"; scope warning at top; Locked Decisions table updated (access gate row, Loki/Tempo/age, drift cron); Success Metrics drop public uptime SLA; Risks rewritten — ChatGPT ban downgraded CRITICAL→HIGH given internal scope; v2 path noted. |
| `phase-10-deploy-hardening.md` | Description rewritten for internal-only; Key Insights add stack-pick lock + drift cron; Functional reqs add 3-option access gate (Cloudflare Access / Tailscale / IP allowlist + WireGuard) — pick one; runbook count 9→10 (drift triage). |
| `phase-09-openai-sdk-compat-tests.md` | Added §"Real-Codex Drift Cron (LOCKED v1)" with `tests/fixtures/canned-prompts.json` spec, full `compat-real-codex.yml` workflow YAML (Sunday 03:00 UTC, 30-min timeout, age-decrypted CHATGPT auth secret, auto-create GH issue on failure), `test_real_codex_drift.py` test file spec. |
| `phase-06-rate-limit-multi-tier.md` | Q2 marked RESOLVED (defer v1.1), accept as known limitation. |

No phase task hydration changes needed — DAG #7–#17 unchanged; the resolutions are in-scope edits to existing phases.

---

## New Risks Introduced

| Risk | Severity | Mitigation |
|---|---|---|
| Internal access gate misconfiguration → public exposure | HIGH | Phase-10 acceptance: external port-scan from third-party host returns ZERO open ports for `/v1/*`. Documented in runbook + checklist. |
| Loki/Tempo/Grafana operator skill | MED | Use Grafana Labs official compose templates as starting point; runbook for retention tuning. |
| Real-codex cron ChatGPT auth secret leak in CI | HIGH | `~/.codex/auth.json` encrypted at rest with `age`; AGE key in GH secret with branch protection; rotate quarterly. |

---

## Open Items After This Round (none blocking)

- **Access gate concrete pick** (Cloudflare Access / Tailscale / IP+WireGuard) — defer to phase-10 step 0; org infra preference, not architectural blocker.
- **Internal domain** (e.g., `codex.internal` or subdomain on existing internal DNS) — phase-10 Caddyfile placeholder; ops fills in.
- **Slack vs PagerDuty severity routing** for alerts — phase-10 alertmanager config; lead picks notification channels.

---

## Status

Plan v1.1 (post-resolutions) ready for cook handoff. All architecture-blocking decisions locked.

**Next step:** `/cook --plan /Users/michaelphan/projects/codex-wrapper/plans/260427-1358-codex-openai-wrapper` → starts at task #7 (phase 00 bootstrap).
