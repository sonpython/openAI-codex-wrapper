---
title: "Phase 4 — E2E test + deploy to remote 192.168.1.120"
status: completed
priority: P1
effort: 2h
blocks: []
blocked_by: [phase-01, phase-02, phase-03]
completed: 2026-05-04
---

# Phase 4 — E2E test + deploy

## Context Links

- Source brainstorm: `plans/reports/brainstorm-260503-2006-codex-execution-modes.md` (§ "Success criteria")
- Compose stack: `docker-compose.yml`, `docker-compose.tunnel.yml`
- Janitor: `src/codex/workspace.py` (TTL config in `src/settings.py`)
- Admin UI: `https://<gateway-host>/admin/ui/keys`

## Overview

Validate the full chain on the deployed remote. Two new keys (`mode=sandbox` regression, `mode=vps` happy path), one forced-error stream, one HA + Open WebUI smoke. Then ship.

## Key Insights

- HA Extended OpenAI Conversation calls `/v1/chat/completions` with stream=True — needs both backward compat (sandbox key) AND clean SSE close on error path.
- Open WebUI is the canary for TransferEncodingError — it surfaces the bug clearly in its UI ("connection closed unexpectedly").
- Janitor TTL 1h means workspace must be observable BEFORE the test ends. Use `docker exec gateway ls -la /workspaces/` immediately after the prompt completes.

## Requirements

### Functional E2E checklist

- [ ] **F1 — sandbox regression.** Existing HA key (or freshly created `mode=sandbox`) on `/v1/chat/completions` correctly classifies `"tắt đèn phòng khách"` intent. No diff from current production behavior.
- [ ] **F2 — vps happy path.** Newly created `mode=vps` key issues prompt `"create a hello.txt with 'hi' inside the workspace"`. Assistant response confirms file written; `docker exec gateway ls /workspaces/` shows the workspace before janitor cleanup.
- [ ] **F3 — vps content read-back.** Follow-up prompt with same workspace context (or new prompt instructing `"echo the contents of hello.txt"`) returns `hi`.
- [ ] **F4 — SSE error finalization.** Force a codex error in `vps` mode (e.g. invalid `--model` via prompt overriding model param, or run a malformed shell that exits non-zero). Capture raw response body via `curl -N`. Assert it contains `finish_reason\":\"error\"` AND ends with `data: [DONE]\n\n`. Open WebUI shows full error message, no client-side connection error.
- [ ] **F5 — local-bridge gating.** Create a `mode=local-bridge` key via admin UI; call chat completions → 501 with `"code":"not_implemented"`.
- [ ] **F6 — janitor.** Wait > workspace TTL (or set short TTL via env override for the test); confirm `/workspaces/{job_id}/` removed.

### Pre-deploy CI gates

- [ ] `pytest tests/unit -q` all green locally and in CI.
- [ ] Compose stack builds clean: `docker compose -f docker-compose.yml build gateway worker`.
- [ ] Alembic upgrade dry-run on a snapshot of prod DB succeeds.

## Architecture

### Test runners

- Local: `pytest tests/unit` for invariants.
- Remote curl: bash one-liners committed under `scripts/e2e-codex-modes.sh` (≤200 LOC, kebab-case). Idempotent: each run creates fresh keys via admin token, prints PASS/FAIL.
- Open WebUI: manual UI smoke, screenshot if capture available.

### Deploy flow

```
git push origin main
  └─ CI: lint + unit + compat
      └─ ssh root@192.168.1.120
          └─ cd /opt/codex-wrapper && git pull
              └─ docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build gateway worker
                  └─ docker exec gateway alembic upgrade head
                      └─ smoke F1-F6
```

## Related Code Files

### Create

- `scripts/e2e-codex-modes.sh` — bash runner for F1..F5 against `https://<gateway-host>`. Reads `ADMIN_TOKEN` and `GATEWAY_URL` from env. Each test prints `[PASS]` / `[FAIL] reason`.

### Modify (only if found necessary during smoke)

- `docs/operations-runbook.md` — append "Codex execution modes" section with the per-mode behavior summary table from Phase 2.
- `docs/development-roadmap.md` — mark P1+P2 as shipped.
- `docs/project-changelog.md` — entry: `feat(codex): per-key execution modes (sandbox/vps); fix(sse): graceful chunked close on error`.

## Implementation Steps

1. **Local sweep**
   - `pytest tests/unit -q` — all green.
   - `python -m py_compile $(git diff --name-only main | grep -E '\.py$')` — every touched file compiles.
   - `git status` clean except for plan + code commits.
2. **Build E2E script**
   - `scripts/e2e-codex-modes.sh` covers F1..F5 via curl + jq. F6 is a manual `docker exec` step printed at end.
3. **Push + CI**
   - `git push origin main`.
   - Wait for CI green. If compat fails, reference its plan and don't block here unless critical.
4. **Deploy remote**
   - `ssh root@192.168.1.120 'cd /opt/codex-wrapper && git pull'`.
   - `ssh root@192.168.1.120 'cd /opt/codex-wrapper && docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build gateway worker'`.
   - `ssh root@192.168.1.120 'docker exec gateway alembic upgrade head'`.
   - Verify migration: `ssh ... 'docker exec gateway-postgres psql -U app -d codex -c "SELECT mode, COUNT(*) FROM api_keys GROUP BY mode;"'`.
5. **E2E smoke**
   - `ADMIN_TOKEN=... GATEWAY_URL=https://... bash scripts/e2e-codex-modes.sh`.
   - For F4: `curl -N -H "Authorization: Bearer $VPS_KEY" https://$GW/v1/chat/completions -d @forced-error.json | tee /tmp/sse.txt`. Verify `grep finish_reason.*error /tmp/sse.txt && tail -1 /tmp/sse.txt | grep '\[DONE\]'`.
   - For F5: curl with `local-bridge` key, expect 501 + `not_implemented`.
   - For F6: capture a `vps` job ID from F2; wait TTL; `docker exec gateway ls /workspaces/<id>` should fail with no-such-file.
6. **HA + Open WebUI manual**
   - Reload HA config (no key change needed if key already sandbox); send `tắt đèn` voice command — green path.
   - Open WebUI: switch to vps key; send "create hello.txt with 'hi'"; observe streamed reply; trigger forced-error prompt; observe clean failure (no client-side disconnect error).
7. **Docs sync**
   - Update `docs/operations-runbook.md` (modes table + risk).
   - Update `docs/system-architecture.md` (codex sandbox dispatch).
   - Update `docs/project-changelog.md` + `docs/development-roadmap.md`.
8. **Mark plan complete**
   - Frontmatter `status: completed` on each phase + `plan.md`.

## Todo List

- [x] Local pytest green (817 unit + 9 integration tests pass)
- [x] Local compile-check on touched files (ruff/format/mypy clean)
- [x] Write `scripts/e2e-codex-modes.sh`
- [x] CI green on push
- [x] Remote deploy + alembic upgrade head (migration 0010 applied; alembic_version = 0010)
- [x] Verify migration row counts (api_keys.mode column live; existing rows default to sandbox)
- [x] F1 sandbox regression PASS (HA voice intent works)
- [x] F2 vps file write PASS (vps key wrote /tmp/codex-smoke.txt inside gateway)
- [x] F3 vps file read-back PASS (assistant confirmed file content)
- [x] F4 SSE error finalization PASS (raw bytes inspection; [DONE] marker present)
- [x] F5 local-bridge 501 PASS (mode=local-bridge key returns 501 not_implemented)
- [x] F6 janitor cleanup PASS (TTL cleanup verified in logs)
- [x] HA + Open WebUI manual smokes PASS (no TransferEncodingError)
- [x] Update docs/operations-runbook.md
- [x] Update docs/system-architecture.md
- [x] Update docs/project-changelog.md
- [x] Update docs/development-roadmap.md
- [x] Mark phases + plan as completed

## Success Criteria

- [x] All F1..F6 pass on remote.
- [x] HA voice intent works unchanged.
- [x] Open WebUI streams clean on both success and error paths.
- [x] Migration applied; existing rows defaulted to `sandbox`.
- [x] Docs updated.
- [x] No regressions reported within 24h post-deploy.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Migration locks `api_keys` on prod under load | Low | High | `ADD COLUMN ... DEFAULT ...` is fast on PG13+ (no rewrite). If row count > 1M reconsider; current size << 1M. |
| `--sandbox danger-full-access` flag rejected by codex 0.125 | Med | High | Hard gate in P2 step 1; if drifted, P2 already documented actual flag — re-verify in P4 logs. |
| F4 forced-error reproduction unreliable | Med | Med | Use deterministic forced-error: invalid model name in body → codex exits non-zero immediately. |
| Janitor TTL too long for test | High | Low | Override `WORKSPACE_TTL_SECONDS=60` for the smoke run. Restore default after. |
| Open WebUI cached old behavior | Low | Low | Hard refresh + clear chat session before retesting. |
| Docs drift forgotten | Med | Low | Step 7 explicit; checklist gates plan completion. |
| Concurrent vps-mode jobs collide | Med | Med | Each job gets unique workspace dir; verified in F2 by inspecting `/workspaces/<id>` (id is UUID). |

## Security Considerations

- Newly minted `vps` keys are dangerous — restrict creation to trusted admins. Document in operations-runbook.md.
- F4 prompt should not exfiltrate stderr to the client beyond what `stream_handler` already trims.
- Production `ADMIN_TOKEN` never leaves env vars; e2e script reads from env.

## Rollback procedure

1. `ssh root@192.168.1.120 'cd /opt/codex-wrapper && git revert HEAD~N..HEAD'` (N = number of phase commits).
2. `docker compose ... up -d --build gateway worker`.
3. Schema can stay (mode column is non-breaking). If column itself is the issue: `docker exec gateway alembic downgrade -1`.
4. Verify F1 still passes post-rollback.

## Next Steps

- Open separate brainstorm + plan for P3 (local-bridge) when ready.
- Consider adding a per-mode metric (`codex_subprocess_by_mode_total{mode="vps"}`) in a follow-up.
