# System Architecture: Modules & Data Flow

**Topic:** Module interactions, detailed data flows, tool-calling synthesis

---

## Tool-Calling Synthesis (Home Assistant Extended OpenAI Conversation)

**Status:** Verified working with HA EOC and nested schemas (v1.0 feature, commit 2091772)

### Problem Solved

Home Assistant Extended OpenAI Conversation's `execute_services` tool declares nested array-of-objects schema:
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "domain": { "type": "string" },
      "service": { "type": "string" },
      "service_data": { "type": "object" }
    },
    "required": ["domain", "service", "service_data"]
  }
}
```

Previous prompt-engineering approach (flat `name(param: type)` format) hid nested keys. Codex emitted JSON without `domain` field → HA raised `KeyError: 'domain'` → "Unexpected error during intent recognition".

### Solution: Full JSON Schema Inlining

`src/chat/tool_calling.py::format_tools_prompt()` now inlines each tool's full `parameters` JSON schema (compact form) in the prompt system message.

```python
def format_tools_prompt(
    tools: list[dict[str, Any]],
    tool_choice: object | None = None,
) -> str:
    """
    Inlines full parameters JSON schema for each tool.
    
    Each tool description includes:
      - {name}: {description}
        parameters: {full JSON schema (compact)}
    
    INSTRUCTIONS block teaches Codex:
      - Emit ONLY JSON (no prose wrapper)
      - Arguments MUST conform exactly to schema
      - For nested objects/arrays: include ALL required keys for EVERY item
    """
```

**Result:** Codex now correctly emits:
```json
{
  "tool_calls": [
    {
      "name": "execute_services",
      "arguments": {
        "list": [
          { "domain": "light", "service": "turn_on", "service_data": {"brightness": 200} },
          { "domain": "climate", "service": "set_temperature", "service_data": {"temperature": 22} }
        ]
      }
    }
  ]
}
```

### Response Parsing

`src/chat/tool_calling.py` provides fallback parsing chain:

1. **Markdown fence stripping** — removes ` ```json ... ``` ` wrapper if Codex adds one
2. **Direct JSON parse** — try `json.loads()` first
3. **JSON object extraction** — fallback: find first `{`, match closing `}` by depth tracking
4. **Plain text pass-through** — if no JSON found, return plain response

### Multi-Turn Tool Use Support

Request schemas (`src/gateway/schemas/chat_request.py`) now support:
- `tool_calls` field on `role="assistant"` messages for history replay
- `tool_call_id` + `role="tool"` for tool result messages (multi-turn flow)

Prompt builder includes tool call history in context.

### Test Coverage

27/27 unit tests in `tests/unit/test_tool_calling.py` pass, including:
- `test_format_tools_prompt_inlines_nested_schema` (regression for HA EOC)
- Multi-tool call synthesis
- Fallback parsing with prose

---

## Chat Completions Data Flow (with Tool-Calling)

### Sync Request Flow

```
1. POST /v1/chat/completions
   {
     "model": "codex",
     "messages": [{"role": "user", "content": "..."}],
     "tools": [...],  # Optional
     "tool_choice": "auto"  # Optional
   }

2. Gateway routes request to chat_completions_sync()
   - Middleware: RequestID, Auth, RateLimit
   
3. chat.sync_handler.sync_chat_completions()
   a. Create workspace: /tmp/workspace-{chat_id}
   b. Validate request (image_url rejected, tool_calls schema validated)
   c. Build prompt via prompt_builder.build_prompt_for_codex()
      - Include tool prompt from format_tools_prompt() if tools present
      - Include multi-turn history if previous tool calls in messages
   d. Spawn codex runner: codex exec --json "{full_prompt}"
   
4. Codex Runner (subprocess)
   Reads prompt with tool definitions + instructions
   Emits JSONL events to stdout
   
5. Parse Response
   - Collect all output events
   - If response is JSON object with "tool_calls" key:
     → Parse as tool calls (verify schema compliance)
     → Set finish_reason = "tool_calls"
   - Else:
     → Plain text response
     → Set finish_reason = "stop"
   
6. Return ChatCompletionsResponse
   {
     "choices": [{
       "message": {
         "role": "assistant",
         "content": null,  # null when tool_calls present
         "tool_calls": [
           {"id": "call_abc", "type": "function", "function": {"name": "...", "arguments": "..."}}
         ]
       },
       "finish_reason": "tool_calls"
     }]
   }
```

### Streaming Request Flow

Same as sync, but returns SSE stream:
```
data: {"choices": [{"delta": {"content": "..."}}]}
data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_abc", ...}]}}]}
data: {"choices": [{"finish_reason": "tool_calls"}]}
```

---

## Real-Codex 0.125 Drift Detection

**Weekly cron:** `compat-real-codex.yml` runs against `@openai/codex@latest` with canned fixtures.

Detects:
- JSONL schema breaks
- Tool-calling response format changes
- Event type mismatches

Auto-files GH issue on failure.

---

## Rate-Limit Model (Detailed)

### Four Dimensions

| Dimension | Window | Storage | Logic |
|-----------|--------|---------|-------|
| **RPM** | Sliding 60s | Redis sorted set + Lua script | Atomic: ZREMRANGEBYSCORE + ZCARD + ZADD |
| **TPM** | Sliding 60s | Redis counter + manual slide | Counter; refresh on window boundary |
| **Concurrent** | Real-time | Redis counter + PEXPIRE 100ms | INCR + PEXPIRE; check before increment |
| **Monthly** | Calendar month | Postgres `usage_counter` + Redis cache | Postgres source-of-truth; Redis cache 1h TTL |

### Lua Script (RPM)

Stored in `src/infra/redis_lua/`:

```lua
-- KEYS[1]: rate_limit_key e.g. "rl:rpm:user-abc:2026-04-29-12"
-- ARGV[1]: now (unix timestamp)
-- ARGV[2]: window_size (seconds, e.g. 60)
-- ARGV[3]: max_requests (e.g. 3600 for 60 req/min)

local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max_req = tonumber(ARGV[3])

-- Remove old entries outside the window
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

-- Count requests in window
local count = redis.call('ZCARD', key)

if count >= max_req then
  return {0, count, max_req}  -- rejected: {0, current_count, limit}
end

-- Add current request
redis.call('ZADD', key, now, now)
redis.call('EXPIRE', key, window + 1)

return {1, count + 1, max_req}  -- accepted: {1, new_count, limit}
```

---

## Workspace & Sandbox (C6 Red-Team Fix)

### Path Validation

```python
def validate_path_inside(requested: str, workspace_root: str) -> Path:
    """Prevent ../../../etc/passwd escape."""
    root_real = os.path.realpath(workspace_root)
    path_real = os.path.realpath(requested)
    
    common = os.path.commonpath([root_real, path_real])
    if common != root_real:
        raise InvalidWorkspacePath(...)
    
    return Path(path_real)
```

Prevents: symlink attacks, hard link escape, `..` traversal.

### Sandbox Enforcement

Codex built-in `--sandbox workspace-write`:
- **Linux ≥5.13:** Landlock (first-class sandboxing)
- **Linux <5.13:** seccomp (syscall filtering)
- **macOS:** Seatbelt (XNU sandbox)

Prevents: network access, system call access, file access outside workspace.

---

**Last Updated:** 2026-04-29
