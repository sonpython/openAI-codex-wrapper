# Brainstorm — Codex Execution Modes (sandbox / vps / local-bridge)

Date: 2026-05-03 20:06 GMT+7
Status: Approved for `/ck:plan` (P1+P2 scope)

## Problem statement

User's prompt "tạo cho mình 1 slide pdf pitch deck" hits two failures:

1. **bwrap namespace error** — Codex's vendored bubblewrap can't create user namespaces inside the gateway Docker container. Both shell exec AND workspace file writes are blocked. Codex itself reports `Terminal trong sandbox hiện đang lỗi ở lớp namespace (bwrap)` and degrades to text-only response.
2. **TransferEncodingError 400** — When codex exits early after the bwrap failure, the gateway closes the SSE stream mid-frame. aiohttp client (Open WebUI / HA) raises `Not enough data to satisfy transfer length header`.

User wants two execution modes:

- **VPS mode** — codex runs with full shell + write access, bound to `/workspaces/{job_id}/` on the gateway VPS. Use case: API consumers (HA, Open WebUI, custom agents) on the deployed instance.
- **Local-bridge mode** (`LOCAL_CODE=1`) — codex's tool calls (read_file, write_file, shell) are routed back to the user's local machine via a persistent connection. Use case: personal dev where codex must touch the user's repo, like Claude Code.

Toggle scope: **per-API-key** (DB column).

## Evaluated approaches

### Mode 1 — VPS execution: how to lift the bwrap restriction

| Approach | Description | Pros | Cons |
|---|---|---|---|
| **A. Pass `--sandbox danger-full-access`** | Disable codex's internal sandbox; trust the Docker container as the boundary. | Simplest. Container is already an isolation layer. Workspace path-bound by convention. Janitor cleans on TTL. | If a prompt injection makes codex `chmod 777 /` inside the container, restart wipes it but during the job other concurrent jobs share the FS. |
| B. Configure Docker for bwrap | Add `cap_add: SYS_ADMIN`, run with `--security-opt apparmor=unconfined`, or use Docker userns-remap to satisfy bwrap's namespace requirement. | Defense-in-depth: container + bwrap. | Higher attack surface (CAP_SYS_ADMIN), more compose complexity, fights against Docker's design. |
| C. Spawn child Docker container per job | Each codex invocation gets its own ephemeral container via Docker socket. | Strongest isolation between jobs. | Heavy: pull image latency, DinD risks, big rewrite of `src/codex/runner.py`. Janitor logic doubled. |

**Decision:** **Approach A**. Aligns with KISS. Docker container IS the sandbox layer. `/workspaces/{job_id}/` is ephemeral + janitor-cleaned. No host bind mounts beyond read-only `/codex-auth`. Acceptable risk for personal/internal use.

### Mode 2 — Local-bridge: how to route tool calls to user's machine

| Approach | Description | Pros | Cons |
|---|---|---|---|
| **A. Persistent WebSocket reverse channel** | Local CLI connects WSS to gateway, gateway routes tool calls back via the same socket. | NAT-friendly (outbound from local). Single connection. Codex still runs on gateway → centralized logging. | Phức tạp: WS protocol design, MCP-style proxy, reconnect logic, heartbeat. Bridge disconnect = stuck job. |
| B. Codex runs on local, gateway logs only | Local CLI runs codex natively; gateway is just an auth/logging passthrough. | Truly local. Zero VPS load. | Defeats the gateway purpose. Codex auth must duplicate on every dev machine. |
| C. SSH reverse tunnel with codex's MCP transport | Reuse codex's MCP server feature; tunnel through SSH-R. | Existing infra reuse. | SSH-R requires user to run `ssh -R` per session — bad UX. |

**Decision:** **Approach A** for P3 (defer). Out of scope for P1+P2.

### Toggle scope

`api_keys.mode` enum column with values `sandbox | vps | local-bridge`. Default `sandbox` (safest). Admin UI dropdown editor + migration to add column. Per-key keeps each caller's mode predictable, doesn't depend on header support in OpenAI clients.

### SSE finalization

When codex exits with non-zero, the chat-completions/responses stream handler must always emit:
- A final `data: {...,"finish_reason":"error"}\n\n` chunk with the partial usage shape clients expect
- Then `data: [DONE]\n\n`
- Then close the chunked stream cleanly (no premature TCP close)

Currently the stream closes after one error log line, leaving aiohttp clients with `TransferEncodingError`. Fix is ~50 LOC in `src/chat/stream_handler.py` and equivalent responses path.

## Final recommended solution

Three-mode design, two phases of work:

| Phase | Scope |
|---|---|
| **P1** | Migration `0010_api_keys_mode_column`; add enum `mode VARCHAR(16)` to `api_keys`, default `sandbox`. Admin UI keys page: dropdown editor (sandbox / vps / local-bridge — last one disabled with "coming soon" note). |
| **P2** | Codex runner reads `api_key.mode`. For `mode=vps`, pass `--sandbox danger-full-access`; for `mode=sandbox`, keep current `--sandbox read-only`. Plus SSE finalization fix in chat + responses stream handlers. Plus regression test hitting both modes through the gateway. |
| P3 | Local-bridge: WebSocket protocol, `codex-wrapper-bridge` CLI, MCP-style tool proxy. Tách brainstorm + plan riêng. |

## Implementation considerations and risks

- **Concurrent jobs in `vps` mode share the gateway container's FS.** Mitigation: each job has its own `/workspaces/{job_id}/` path; janitor TTL 1h; codex working dir locked to that subtree by `--cwd` (or equivalent).
- **Prompt injection risk in `vps` mode.** Acceptable for v1 (personal use). Document in `docs/operations-runbook.md`. Future: prompt-level allowlist or content scanner.
- **Mode rollout:** existing keys default to `sandbox`. User must explicitly upgrade their HA/OW key to `vps` via admin UI.
- **SSE fix is a stability win independent of modes** — any error in codex (not just bwrap) currently produces TransferEncodingError on aiohttp clients.
- **Backward compat:** `mode=sandbox` keeps existing behavior bit-identical, so migrating to the schema change is non-breaking.

## Success criteria

- [ ] User creates a `vps` tier key in admin UI
- [ ] Calling `POST /v1/chat/completions` from Open WebUI with that key against the deployed gateway successfully writes a file under `/workspaces/{job_id}/` and returns its content (e.g. "create a hello.txt with 'hi' inside")
- [ ] Open WebUI shows full streamed response with no TransferEncodingError on either success or codex-error paths
- [ ] HA Extended OpenAI Conversation continues to work unchanged with `mode=sandbox` keys
- [ ] Janitor confirmed cleaning workspaces older than 1h on the deployed remote

## Next steps + dependencies

1. Run `/ck:plan` with this report as context — produces `plans/{date}-codex-execution-modes-p1p2/` with phase files
2. Execute P1+P2 via `/ck:cook`
3. Deploy + verify on remote 192.168.1.120 (existing tunnel + admin UI)
4. Open separate brainstorm for P3 (local-bridge) when ready

## Unresolved questions

- Does codex CLI 0.125 support `--sandbox danger-full-access` exactly, or is the flag named differently? (Verify with `codex exec --help` on remote during P2 implementation.)
- Should `mode=vps` keys also unlock codex's network access (currently restricted by bwrap layer)? — defer until first user need.
- For multi-tenant deployments (future), is per-job container needed? — out of scope for personal-use v1.
