---
title: "Codex execution modes (sandbox / vps) + SSE finalization"
description: "Per-API-key mode column to unlock VPS write-access codex runs, plus SSE close-frame fix for aiohttp clients."
status: pending
priority: P1
effort: 8h
branch: main
tags: [codex, sandbox, sse, admin-ui, schema]
created: 2026-05-03
---

# Codex Execution Modes — P1 + P2

## Source of truth

- Brainstorm: `plans/reports/brainstorm-260503-2006-codex-execution-modes.md`
- P3 (local-bridge): out of scope; separate plan later.

## Goal

Two outcomes:

1. **Mode dispatch.** Per-API-key column `api_keys.mode` ∈ `{sandbox, vps, local-bridge}`. Codex runner reads mode and selects the matching `--sandbox` policy (`read-only` for `sandbox`, `danger-full-access` for `vps`, 501 for `local-bridge`).
2. **SSE finalization.** Stream handlers always emit a final `finish_reason="error"` chunk + `[DONE]` + clean chunked-close before the response body ends, eliminating `TransferEncodingError` on aiohttp clients (Open WebUI, HA Extended OpenAI Conversation) when codex exits non-zero.

## Phases

| # | Phase | Status | Effort | Blocks |
|---|---|---|---|---|
| 1 | [Schema + admin UI](./phase-01-schema-and-admin-ui.md) | pending | 2h | — |
| 2 | [Codex runner mode dispatch](./phase-02-codex-runner-mode-dispatch.md) | pending | 2.5h | P1 |
| 3 | [SSE stream finalization](./phase-03-sse-stream-finalization.md) | pending | 1.5h | — (independent) |
| 4 | [E2E test + deploy](./phase-04-e2e-test-and-deploy.md) | pending | 2h | P1, P2, P3 |

Phase 3 can run parallel with 1+2. Phase 4 depends on all three.

## Dependencies

- Postgres reachable (alembic upgrade head)
- Remote `192.168.1.120` SSH access for `codex exec --help` flag verification + deploy
- Codex binary 0.125 inside the gateway container

## Key constraints

- YAGNI / KISS / DRY. No new env knobs beyond what brainstorm describes.
- Files ≤ 200 LOC.
- Conventional commits, no AI references.
- `--sandbox=danger-full-access` MUST be verified live in P2 before commit; fall back to actual flag name if drifted.
- Backward compat: existing keys default to `sandbox` → bit-identical behavior.

## Success criteria (rolled up)

- [ ] Migration `0010` applies cleanly fwd + reversibly down; existing rows get `mode='sandbox'`.
- [ ] Admin UI creates `vps` keys; `local-bridge` option visible but disabled.
- [ ] `mode=vps` key writes a file under `/workspaces/{job_id}/` and assistant returns it.
- [ ] `mode=sandbox` key continues to classify HA "tắt đèn" intent.
- [ ] Forced codex exit-1 stream: client receives `finish_reason="error"` + `[DONE]` + clean close (no TransferEncodingError).
- [ ] All `pytest tests/unit -q` green.
- [ ] Deployed remote 192.168.1.120; janitor still cleans `/workspaces/` after TTL.

## Rollback plan

- P1: `alembic downgrade -1` drops the column. UI dropdown is additive — old templates still render.
- P2: revert runner.py + middleware patch. `mode=sandbox` is the safe default — all keys continue to work read-only.
- P3: revert stream_handler.py patches; existing tests still pass (no regression in success path).
- P4: deploy is `git revert` + redeploy; no schema rollback needed if P1 stays.

## Docs impact

`major` — `docs/system-architecture.md` (mode dispatch flow), `docs/operations-runbook.md` (per-mode behavior + risk note).
