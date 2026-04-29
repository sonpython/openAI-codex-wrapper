# Brainstorm: Simple Tool Calling for HA Integration

**Date:** 2026-04-29 03:49 GMT+7
**Trigger:** Real e2e against HA Extended OpenAI Conversation showed tool_calls field accepted but no action taken (codex doesn't natively call tools).
**Goal:** Synthesize tool calling via prompt-engineering so HA voice commands + Q&A + automation gen all work end-to-end through codex wrapper.

---

## Problem Statement

Codex CLI has no native function calling. HA Extended OpenAI Conversation sends `tools: [{type:"function", function:{name, parameters,...}}]` and expects responses with `choices[].message.tool_calls[]`. Without this:
- Voice commands ("turn off lights") → codex chats but doesn't invoke HA service
- Q&A about state ("what's the temperature?") → codex hallucinates instead of querying real state
- Automation gen → only works because it's pure text gen

User wants ONE wrapper handling all 3 use cases at MVP-grade reliability (~75-80%).

---

## Locked Decisions

| Area | Decision |
|---|---|
| Use case | Full HA assistant: voice + Q&A + automation gen |
| Reliability target | 70-80% MVP (acceptable for personal/internal HA) |
| Approach | **Prompt-engineering JSON pipeline** (no codex changes) |
| Effort budget | ~4 hours (one session) |
| Streaming tool_calls | Defer to v1.1 — sync only v1 |
| Multiple tool calls/turn | **Support N parallel calls** v1 |
| Invalid tool fallback | **Plain text response** (don't pretend tool exists) |

---

## Architecture

### Request flow (chat-completions only — Responses API skip for now)

```
HA Request {messages, tools, tool_choice}
  ↓
Wrapper detects tools field non-empty
  ↓
Build augmented system prompt:
  "Available tools (only call when user request requires action):
  - turn_on(entity_id: str): turn on a device
  - get_state(entity_id: str): query current state
  - ...

  INSTRUCTIONS:
  - To call tool(s), reply ONLY with this JSON shape:
    {\"tool_calls\":[{\"name\":\"...\", \"arguments\":{...}}, ...]}
  - For multiple actions, include multiple objects in the array.
  - If no tool needed, reply naturally as plain text.
  - NEVER mix JSON with prose."
  ↓
Build user prompt from messages history (handle role=tool feedback msgs)
  ↓
codex exec --json (existing pipeline)
  ↓
Parse codex agent_message text:
  - Strip ```json fences
  - json.loads()
  - Validate shape: {tool_calls: list of {name, arguments}}
  - Validate each tool name against requested tools list
  ↓
  ├─ Valid JSON + valid tool names
  │   → Build response: {choices: [{message: {role:assistant,
  │     content: null, tool_calls: [{id:call_xxx, type:function,
  │     function:{name, arguments: json.dumps(args)}}]},
  │     finish_reason: "tool_calls"}]}
  │
  └─ Invalid (parse fail / unknown tool / no tools list / mixed)
      → Fall back to plain text response (existing path)
      → finish_reason: "stop"
```

### Multi-turn (HA sends tool result back)

HA next request has these messages:
1. user: "turn off lights"
2. assistant: tool_calls=[{name:"turn_off", args:{entity_id:"light.living"}}]
3. tool: {tool_call_id:"call_xxx", name:"turn_off", content:"{\"success\":true}"}
4. user: (or empty for follow-up)

Wrapper formats prompt as:
```
User: turn off lights
Assistant called tool: turn_off({"entity_id":"light.living"})
Tool turn_off result: {"success":true}
Assistant:
```

Codex sees full context and replies "Done, lights are off" (or similar).

### Module changes

| File | Change | LOC | Status |
|---|---|---|---|
| `src/chat/tool_calling.py` | NEW: format_tools_prompt + parse_tool_response + format_history_for_prompt | ~180 | new |
| `src/chat/prompt_builder.py` | Handle role=tool messages; integrate tool_calling module | +40 | mod |
| `src/chat/sync_handler.py` | Branch on `request.tools`: tool-mode vs text-mode | +60 | mod |
| `src/gateway/schemas/chat_response.py` | ToolCall + ToolCallFunction pydantic models | +35 | mod |
| `src/gateway/schemas/chat_request.py` | Allow `role="tool"` in Message; tool_call_id optional field | +20 | mod |
| `src/chat/id_factory.py` | `make_tool_call_id() -> "call_<24 hex>"` | +5 | mod |
| `tests/unit/test_tool_calling.py` | NEW: parse + format + validate cases | ~150 | new |

---

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Codex inconsistent JSON formatting (sometimes wraps in markdown, sometimes adds prose) | HIGH | Parser strips `\`\`\`json` fences; tries multiple parse strategies; falls back to text on failure |
| Codex hallucinates tool names | MED | Validate tool name against request's tools list; fallback to text on unknown |
| Token bloat from tools schema in prompt (each call costs ~500-2000 extra tokens) | MED | Only inject tool prompt when `tools` non-empty; document in README |
| Vietnamese commands inconsistent | MED | Test 5-10 real Vietnamese phrases during impl; adjust prompt template |
| Tool args type mismatch (e.g. "25" vs 25 for number param) | LOW | Pass arguments as string per OpenAI spec — HA does type coercion on its side |
| Multi-tool calls confused order | LOW | Codex usually emits in logical order; HA executes in array order |
| Streaming + tool_calls deferred → some clients break | LOW | Sync mode satisfies HA + Open WebUI; streaming tool_calls v1.1 if needed |

---

## Implementation Plan (~4h)

| Phase | Task | Time |
|---|---|---|
| 1 | Prompt template design + iterate against real codex (5 prompts) | 30m |
| 2 | `tool_calling.py` implementation: format + parse + validate | 60m |
| 3 | Wire into `sync_handler.py` + schema additions | 45m |
| 4 | Unit tests for parse cases (8+ scenarios) | 60m |
| 5 | Docker rebuild + manual test from Open WebUI + HA | 30m |
| 6 | Vietnamese command test + prompt tweak | 20m |
| 7 | Commit + brief docs | 15m |

---

## Success Criteria

- HA Extended OpenAI Conversation returns successful actions for 7+ of 10 test commands
- 5+ test prompts in Vietnamese work
- Multi-turn (call → result → user follow-up) works coherently
- Existing 613 unit tests still pass; 15+ new tool-calling tests pass
- No regression in non-tools chat-completions path

---

## Out of Scope (defer)

1. Streaming tool_calls (state machine for partial JSON delta) → v1.1
2. `tool_choice="required"` enforcement (force tool call) → v1.1
3. Responses API tool_calls (different event taxonomy) → v1.1
4. `parallel_tool_calls=false` honor (sequential rather than batch) → v1.1
5. Function definition validation (ensure declared schema matches HA's actual service) → v1.1
6. Vietnamese language explicit prompt tuning beyond initial test → v1.1

---

## Unresolved Questions

1. **Should we log full tool prompts to audit_log?** Default false (privacy + token volume). Make env-toggle `AUDIT_LOG_TOOL_PROMPTS=false`.
2. **Vietnamese-first prompt language?** v1: English instructions, accept Vietnamese user messages. v1.1: tune Vietnamese instructions if accuracy gap observed.
3. **Tool validation strictness for HA-only deployment?** v1 strict (reject unknown). v1.1 may relax if HA exposes dynamic services.

---

## Acceptance

- 75-80% accuracy across 10 test scenarios (HA voice + Q&A + automation)
- Vietnamese ≥ 60% accuracy (lower bar acceptable)
- No regression in existing test suite (615 tests)
- Open WebUI still works (no tools sent → text mode unchanged)

---

## Status

Ready for `/ck:plan` to expand into detailed implementation phases, OR proceed directly to `/cook` since scope is small (4h, single sub-feature on existing chat-completions endpoint).
