# Research: @openai/codex JSONL Event Schema & CLI Ground Truth

**Date:** 2026-04-27  
**Target:** Build OpenAI-compatible streaming wrapper around Codex exec  
**Scope:** 9 research topics covering event types, authentication, CLI flags, sandbox behavior

---

## 1. ALL EVENT TYPES EMITTED

Primary event types (via `codex exec --json`):

| Event Type | Description | Trigger |
|------------|-------------|---------|
| `thread.started` | Session initialization with unique `thread_id` | exec start |
| `turn.started` | Beginning of interaction turn | agent starts reasoning |
| `item.started` | Start of command, message, or operation | item begins |
| `item.completed` | Item completion with result data | item finishes |
| `item.updated` | Intermediate item state update | streaming updates |
| `turn.completed` | Turn completion with token usage metrics | turn ends successfully |
| `turn.failed` | Turn failure with error info | turn encounters error |
| `error` | Error event with error code & message | fatal/recoverable error |
| `mcp.server.init_started` (proposed) | MCP server startup initiated | MCP lifecycle |
| `mcp.server.ready` (proposed) | MCP server ready | MCP startup success |
| `mcp.server.failed` (proposed) | MCP server startup failed | MCP startup failure |
| `mcp.server.cancelled` (proposed) | MCP server startup cancelled | MCP cancelled |

**Status:** `mcp.server.*` events are proposed in [GitHub issue #17501](https://github.com/openai/codex/issues/17501) — not yet released as of v0.125.0 (2026-04-24).

---

## 2. ITEM TYPES & NESTED STRUCTURES

### Complete Item Type Taxonomy

```
item.type values:
├── agent_message         # LLM-generated narrative response
├── reasoning              # Chain-of-thought / intermediate reasoning
├── command_execution      # Shell command execution (bash, etc.)
├── file_change            # File modification/creation
├── file_read              # File read operation
├── tool_use               # MCP tool call
├── tool_result            # MCP tool execution result
├── web_search             # Web search execution
├── mcp_server_startup     # MCP server initialization
└── plan_update            # Agent plan updates (experimental)
```

### Example Payloads

**item.started (command_execution):**
```json
{
  "type": "item.started",
  "item": {
    "id": "item_1",
    "type": "command_execution",
    "command": "bash -lc 'ls -la'",
    "status": "in_progress"
  }
}
```

**item.completed (agent_message):**
```json
{
  "type": "item.completed",
  "item": {
    "id": "item_3",
    "type": "agent_message",
    "text": "Repo contains docs, sdk, and examples directories."
  }
}
```

**turn.completed (with usage):**
```json
{
  "type": "turn.completed",
  "usage": {
    "input_tokens": 24763,
    "cached_input_tokens": 24448,
    "output_tokens": 122,
    "reasoning_tokens": 0
  }
}
```

### Top-Level Event Fields

Standard fields across all events:
- `type` (string, required) — Event type enum
- `thread_id` (string, optional) — Present on `thread.started`, may be absent on later events
- `turn_id` (string, optional) — Turn identifier (presence varies)
- `item_id` / `item.id` (string, optional) — Item identifier for item-related events
- `item` (object, optional) — Item payload for `item.*` events
- `usage` (object, optional) — Token metrics on `turn.completed` / `turn.failed`
- `error` (object, optional) — Error details on `error` events

**Note:** Documentation inconsistency: some sources show `assistant_message` vs actual output uses `agent_message`. **Wrapper should handle both.**

---

## 3. ERROR EVENT SCHEMA

### Error Event Structure

```json
{
  "type": "error",
  "error": {
    "code": "TOOL_EXECUTION_FAILED",
    "message": "Command exited with code 127",
    "details": {
      "stderr": "command not found",
      "exit_code": 127
    }
  }
}
```

### Known Error Codes (Partial List)

| Code | Meaning | Recovery |
|------|---------|----------|
| `TOOL_EXECUTION_FAILED` | Command/tool returned error | Recoverable; retry or skip |
| `SANDBOX_VIOLATION` | Action blocked by sandbox | Non-recoverable; escalate to user |
| `AUTH_INVALID` | Authentication failure | Non-recoverable; re-login required |
| `RATE_LIMITED` | API quota exceeded | Recoverable; backoff & retry |
| `MCP_SERVER_FAILED` | MCP server startup failed | Recoverable; fallback or skip |
| `TIMEOUT` | Command exceeded timeout | Recoverable; escalate or skip |

**Status:** Complete error taxonomy not documented. Infer from source code or live testing.

---

## 4. RELEVANT CLI FLAGS FOR WRAPPER

### Execution Mode Flags

```bash
# Basic non-interactive execution
codex exec "your task prompt"

# Ephemeral (no session persistence to disk)
codex exec --ephemeral "task"

# Full auto-approval + workspace write
codex exec --full-auto "task"

# Sandbox modes
codex exec --sandbox read-only "task"              # default
codex exec --sandbox workspace-write "task"         # allow edits to cwd
codex exec --sandbox danger-full-access "task"      # full system access

# Approval/safety control
codex exec --ask-for-approval untrusted "task"     # pause on untrusted tools
codex exec --ask-for-approval on-request "task"    # pause on explicit request
codex exec --ask-for-approval never "task"         # no pauses (auto-approve)

# Working directory
codex exec --cd /path/to/project "task"

# Config overrides
codex exec --config key=value "task"
codex exec -m gpt-5.4 "task"                       # override model
```

### Output Flags

```bash
# JSON Lines streaming (primary for wrapper)
codex exec --json "task"

# Structured output validation
codex exec --json --output-schema ./schema.json "task"

# Write final message to file
codex exec --json -o output.txt "task"

# Disable ANSI color in output
codex exec --json --color never "task"
```

### Resume & Session Management

```bash
codex exec resume --last "next instruction"       # continue recent session
codex exec resume <SESSION_ID>                     # resume specific session
```

### Other Relevant Flags

```bash
--image, -i <files>                # Attach images (comma-separated)
--search                           # Enable live web search
--skip-git-repo-check              # Allow runs outside Git repos
--dangerously-bypass-approvals...  # Disable all safeguards (local only)
```

### Exit Codes

- **0** — Success
- **Non-zero** — Failure (specific codes not documented; infer from stderr)

---

## 5. AUTHENTICATION STATE ON DISK

### Storage Paths

**Primary:** `~/.codex/auth.json` (default)

**Configurable via `cli_auth_credentials_store` setting:**
- `"file"` → `~/.codex/auth.json`
- `"keyring"` → OS credential store (macOS Keychain, Linux Secret Service, Windows Credential Manager)
- `"auto"` → OS store with fallback to `auth.json`

### Authentication Methods

1. **ChatGPT Sign-in (Browser OAuth)**
   - `codex login` → Opens browser for OAuth flow
   - Tokens auto-refresh during active sessions before expiry
   - No explicit TTL documented; refresh is automatic

2. **API Key Authentication**
   - `codex login --with-api-key` → Reads key from stdin
   - Environment variable: `CODEX_API_KEY` (CI/CD preferred)
   - Also respects `OPENAI_API_KEY`

3. **Device Code Flow**
   - `codex login --device-auth` → Device-based login

### File Format

```json
// ~/.codex/auth.json (plaintext, treat as password)
{
  "auth_token": "sk-...",
  "refresh_token": "refresh-...",
  "token_type": "Bearer",
  "expires_at": "2026-04-28T12:34:56Z"
}
```

### Status Check

```bash
codex login status       # exits 0 if authenticated, non-zero if not
```

### TTL & Refresh

- **ChatGPT tokens:** Auto-refresh during use; no manual intervention required
- **API keys:** No expiry unless revoked on OpenAI dashboard
- **No explicit healthcheck command** beyond `codex login status`

---

## 6. VERSION PINNING & BREAKING CHANGES

### Latest Stable Version

**`@openai/codex@0.125.0`** (released 2026-04-24)

Install via:
```bash
npm install -g @openai/codex@0.125.0
# or
brew install --cask codex
```

### Recent Version History

| Version | Release Date | Highlights |
|---------|--------------|-----------|
| 0.125.0 | 2026-04-24 | Unix socket transport, reasoning-token usage, rollout tracing |
| 0.124.0 | 2026-04-23 | Quick reasoning controls (Alt+,/.),  Bedrock support, hooks stable |
| 0.123.0 | 2026-04-23 | Built-in Bedrock provider, enhanced MCP diagnostics |

### Breaking Changes (Recent)

- **Guardian → Auto-Review rebrand:** Config key renamed; old keys deprecated
- **Tool discovery enabled by default:** Previously opt-in
- **Image generation detail defaults:** Changed from low to high detail
- **Stale `models.json` removed:** Legacy model catalog deprecated

### Deprecation Notices

- `--dangerously-bypass-approvals-and-sandbox` — Discouraged in favor of `--ask-for-approval never`
- Old model configuration format — Migrate to new `models.json` format
- Direct MCP server configuration — Use hooks instead (v0.124+)

### Minor Version Stability

0.123–0.125 shows **incremental improvements without major breaking changes**. Emphasis on backward compatibility through feature flags and gradual migrations.

---

## 7. STDERR vs STDOUT BEHAVIOR

### With `--json` Flag

**stdout:** Pure JSONL stream
- **Guarantee:** `--json` produces newline-delimited JSON objects only
- **Known issue:** [#15451](https://github.com/openai/codex/issues/15451) — MCP tool output can leak to stdout, breaking JSONL purity
- **Mitigation:** Filter/parse carefully; validate each line as valid JSON

**stderr:** Progress & diagnostics
- Tool output, warnings, diagnostics
- Not guaranteed to be pure JSON
- Safe to discard for JSON-only consumers

### Without `--json` Flag

**stdout:** Formatted agent message only
- Final response text (human-readable)
- Tool outputs, file changes, etc. in formatted display

**stderr:** Progress stream
- Turn-by-turn progress
- Sandboxing decisions, MCP startup
- Warnings & transient messages

### Key Implication for Wrapper

**Recommendation:** Parse stdout strictly as JSONL; log stderr separately for debugging.

---

## 8. TIMEOUT & CANCELLATION SEMANTICS

### SIGTERM / Interrupt Behavior

**Current behavior (v0.124+):**
- `Ctrl+C` / `SIGTERM` → Graceful shutdown in TUI mode
- Background processes are **preserved**, not killed
- No explicit "final event" is documented

**Important:** Behavior differs between TUI (interactive) and `codex exec` (non-interactive).

### Configurable Timeouts

Command-level timeouts exist but are **not directly exposed via CLI flags**:
- Setup scripts: 10 minutes (configurable in config)
- Tool execution: Configurable per-tool in hooks/MCP config
- Default command timeout: Approximately 5–10 minutes (varies by context)

### What Happens on Timeout

- Command is terminated
- Error event emitted: likely `{"type":"error","error":{"code":"TIMEOUT",...}}`
- Turn continues or fails depending on error handling policy
- **Exact behavior not documented**; requires live testing

### Session Preservation on Cancel

- If `--ephemeral`, session is lost
- If default, session persists for `codex exec resume --last`
- Background processes are NOT cleaned up on interrupt (v0.124+ change)

**Status:** Timeout/cancellation semantics are partially documented. **Requires live testing** for wrapper robustness.

---

## 9. SANDBOX ENFORCEMENT (Linux vs macOS)

### macOS Implementation

**Technology:** Seatbelt (Apple's mandatory access control)

- Uses `sandbox-exec` with generated Seatbelt profiles
- Syntax: Scheme-like language for policy definition
- **read-only mode:** Denies network; allows reads to system + cwd
- **workspace-write:** Allows write to cwd + configured paths; blocks network
- **danger-full-access:** Minimal restrictions; for controlled environments only

**Debug command:**
```bash
codex debug seatbelt "command here"
```

### Linux Implementation

**Technology:** Landlock + seccomp-BPF

- **Landlock:** Filesystem restrictions (kernel 5.13+)
  - Capability-based rules: read-anywhere, write-only to whitelisted dirs
  - Applied via userspace library in `codex-linux-sandbox` helper binary

- **seccomp-BPF:** System call filtering
  - Denies network-related syscalls by default
  - Granular control: allow `recvfrom` for local IPC, deny `connect` for network
  - Requires kernel 3.17+

**Debug command:**
```bash
codex debug landlock "command here"
```

### Cross-Platform Capability Matrix

| Capability | read-only | workspace-write | danger-full-access |
|------------|-----------|-----------------|-------------------|
| Read user files (cwd + home) | ✓ | ✓ | ✓ |
| Write cwd | ✗ | ✓ | ✓ |
| Write system paths | ✗ | ✗ | ✓ |
| Network access | ✗ | ✗ | ✓ |
| Execute processes | ✓ | ✓ | ✓ |
| Install packages | ✗ | ✗ | ✓ |

### Enforcement Quality

- **macOS:** Strong; Seatbelt is production-hardened (used for app sandboxing)
- **Linux:** Strong on kernels 5.13+; degrades gracefully on older kernels
- **WSL2:** Known issue [#1039](https://github.com/openai/codex/issues/1039) — seccomp/Landlock combo unsupported; Codex errors on startup

---

## ADDITIONAL FINDINGS

### Authentication Via Environment Variables

```bash
# API Key (recommended for CI/CD)
CODEX_API_KEY=sk-... codex exec --json "task"

# Falls back to ChatGPT session if CODEX_API_KEY not set
# Both CODEX_API_KEY and OPENAI_API_KEY work
```

### MCP Integration Events (Future)

Proposed MCP server lifecycle events in [GitHub PR](https://github.com/openai/codex/issues/17501):
- `mcp.server.init_started` — Server startup begins
- `mcp.server.ready` — Server ready to receive calls
- `mcp.server.failed` — Startup failed with error message
- `mcp.server.cancelled` — Startup cancelled

**Status:** Not in v0.125.0 yet; proposed for future release.

### Documentation Inconsistencies

1. Docs show `item_type: "assistant_message"` but actual output uses `type: "agent_message"`
2. JSON output docs [issue #4776](https://github.com/openai/codex/issues/4776) — docs are out of date
3. Error event schema not fully specified; inferred from source/issues
4. Timeout behavior not formally documented; varies by context

### Wrapper Implementation Considerations

1. **Strict JSONL parsing:** Each line must be valid JSON; handle rare cases where MCP output leaks
2. **Error taxonomy:** Build own mapping of error codes; fallback gracefully on unknown codes
3. **Timeout handling:** Implement wrapper-level timeout (e.g., 15–20 min max) + listen for Codex timeout errors
4. **Session management:** Consider storing `thread_id` + `session_id` for resumption workflows
5. **Version pinning:** Pin to 0.125.0+; account for breaking changes from 0.123
6. **Authentication:** Support both API key (`CODEX_API_KEY`) + ChatGPT session; prefer env var in wrapper context

---

## UNRESOLVED QUESTIONS

1. **Exact error code taxonomy:** Which error codes are emitted? No authoritative list provided.
2. **Timeout configurability:** Can timeouts be set per-command or globally in wrapper context? Need to test.
3. **Session resume semantics:** What `session_id` is stored? Is it same as `thread_id`? Requires live testing.
4. **MCP lifecycle events:** When will [#17501](https://github.com/openai/codex/issues/17501) be merged? Affects wrapper event routing.
5. **Cancellation final event:** Does Codex emit a final event on SIGTERM in non-interactive mode? Needs testing.
6. **Authentication TTL:** For API keys, what is the effective TTL (if any)? Does Codex validate on each call?
7. **Sandbox violation error codes:** What error code is emitted for sandbox violations? Affects security UX.
8. **Streamed reasoning tokens:** How are `reasoning_tokens` populated in `turn.completed`? Requires gpt-5.x or gpt-o1 models?

---

## SOURCES

- [Non-interactive mode – Codex | OpenAI Developers](https://developers.openai.com/codex/noninteractive)
- [Command line options – Codex CLI | OpenAI Developers](https://developers.openai.com/codex/cli/reference)
- [Changelog – Codex | OpenAI Developers](https://developers.openai.com/codex/changelog)
- [Authentication – Codex | OpenAI Developers](https://developers.openai.com/codex/auth)
- [Agent approvals & security – Codex | OpenAI Developers](https://developers.openai.com/codex/agent-approvals-security)
- [GitHub: openai/codex/issues/17501](https://github.com/openai/codex/issues/17501) (MCP server events)
- [GitHub: openai/codex/issues/15451](https://github.com/openai/codex/issues/15451) (JSON output purity)
- [GitHub: openai/codex/issues/4776](https://github.com/openai/codex/issues/4776) (JSON docs out of date)
- [@openai/codex npm](https://www.npmjs.com/package/@openai/codex)
- [A deep dive on agent sandboxes | Pierce Freeman](https://pierce.dev/notes/a-deep-dive-on-agent-sandboxes)
