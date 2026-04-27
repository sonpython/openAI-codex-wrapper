# OpenAI Streaming Event Taxonomy & Payload Reference

**Research Date:** 2026-04-27  
**Scope:** Chat Completions streaming + Responses API streaming  
**Goal:** Exact wire-format documentation for byte-for-byte compliance with official OpenAI SDKs

---

## Part A: Chat Completions Streaming API

### A.1 SSE Wire Format & Headers

#### Response Headers
```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Transfer-Encoding: chunked
Connection: keep-alive
```

#### Event Format
Each event is a data-only SSE:
```
data: <JSON_OBJECT>
<blank line>

data: <JSON_OBJECT>
<blank line>

data: [DONE]
<blank line>
```

- **Separator:** `\n\n` (double newline) between events
- **Terminator:** Final event is literally `data: [DONE]` followed by `\n\n`
- **JSON Parsing:** Each `data: ` prefix must be stripped; rest is valid JSON except `[DONE]`
- **No "event:" line** — chat completions uses data-only SSE (unlike Responses API)

---

### A.2 ChatCompletionChunk Schema (Request: `stream: true`)

#### Top-Level Fields
| Field | Type | Always Present | Notes |
|-------|------|-----------------|-------|
| `id` | string | Yes | Same across all chunks in stream |
| `object` | `"chat.completion.chunk"` | Yes | Literal constant |
| `created` | int | Yes | Unix timestamp (seconds); same across all chunks |
| `model` | string | Yes | Model name from request |
| `choices` | array | Yes | Length typically 1 unless `n > 1`; can be empty on final usage chunk |
| `system_fingerprint` | string | No | Backend config fingerprint for reproducibility |
| `usage` | CompletionUsage | No | Only present if `stream_options: {"include_usage": true}` is set; null on all chunks except last |
| `service_tier` | string | No | Processing tier: `auto`, `default`, `flex`, `scale`, `priority` |

#### Choice Object (in `choices[]`)
| Field | Type | Notes |
|-------|------|-------|
| `index` | int | Position in choices array (0 for single completion) |
| `delta` | ChoiceDelta | The streamed content chunk |
| `finish_reason` | string? | Null on all but final chunk; values: `stop`, `length`, `tool_calls`, `content_filter`, `function_call` |
| `logprobs` | ChoiceLogprobs? | Token probability info (if `logprobs: true` in request) |

#### ChoiceDelta Object (in `choices[].delta`)
| Field | Type | When Present | Notes |
|-------|------|--------------|-------|
| `role` | string | First chunk only | `"assistant"` typically; can be other roles |
| `content` | string? | Middle & final chunks | Text delta; `null` if no text in this chunk |
| `tool_calls` | array? | When model calls tools | Array of partial tool call objects |
| `refusal` | string? | If content filtered | Reason for refusal (if any) |

#### ChoiceDelta → tool_calls[] (When Present)
Each tool call delta contains:
```json
{
  "index": 0,           // Position in tool_calls array
  "id": "call_abc...",  // Only on first delta for this call
  "type": "function",   // Always "function"
  "function": {
    "name": "get_weather",    // Only on first delta
    "arguments": "{\n  \"lo"   // JSON string, streamed incrementally
  }
}
```
- **Arguments Field:** Streamed as partial JSON string; fully assembled only when `finish_reason !== null`
- **Multiple Calls:** If model calls `N` tools, you get deltas for all N (track by `index`)

#### ChoiceLogprobs Object (if present)
```json
{
  "content": [
    {
      "token": "The",
      "logprob": -0.123,
      "bytes": [84, 104, 101],  // UTF-8 bytes
      "top_logprobs": [
        {"token": "the", "logprob": -0.123},
        {"token": "The", "logprob": -0.456}
      ]
    }
  ]
}
```

#### CompletionUsage Object (Final Chunk Only)
Only emitted when `stream_options: {"include_usage": true}`:
```json
{
  "prompt_tokens": 10,
  "completion_tokens": 15,
  "total_tokens": 25,
  "prompt_tokens_details": {
    "cached_tokens": 0,
    "audio_tokens": 0
  },
  "completion_tokens_details": {
    "reasoning_tokens": 0,
    "audio_tokens": 0
  }
}
```
- Present as `null` on all chunks **except the last one**
- **Final chunk special:** `choices: []` (empty) and `usage` populated
- If stream cancelled before completion, final chunk may never arrive

---

### A.3 Chunk Evolution: First, Middle, Final

#### First Chunk (index 0)
```json
{
  "id": "chatcmpl-123abc",
  "object": "chat.completion.chunk",
  "created": 1704067200,
  "model": "gpt-4o",
  "system_fingerprint": "fp_abc123",
  "choices": [
    {
      "index": 0,
      "delta": {
        "role": "assistant",
        "content": "Hello"
      },
      "finish_reason": null,
      "logprobs": null
    }
  ],
  "usage": null
}
```
- **`role` only here** (subsequent chunks omit it)
- `finish_reason` is `null`
- `usage` is `null` (or omitted)

#### Middle Chunk
```json
{
  "id": "chatcmpl-123abc",
  "object": "chat.completion.chunk",
  "created": 1704067200,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "delta": {
        "content": " how are"
      },
      "finish_reason": null
    }
  ]
}
```
- **No `role`** in delta
- `finish_reason` is `null`
- Only content changes

#### Final Chunk (No Usage)
```json
{
  "id": "chatcmpl-123abc",
  "object": "chat.completion.chunk",
  "created": 1704067200,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "delta": {
        "content": " you?"
      },
      "finish_reason": "stop"
    }
  ]
}
```
- **`finish_reason` non-null:** `"stop"`, `"length"`, `"content_filter"`, `"tool_calls"`, `"function_call"`
- Content may be empty `""` if stream ended

#### Final Chunk (With Usage)
When `stream_options: {"include_usage": true}`:
```json
{
  "id": "chatcmpl-123abc",
  "object": "chat.completion.chunk",
  "created": 1704067200,
  "model": "gpt-4o",
  "choices": [],  // ALWAYS EMPTY on usage final chunk
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "total_tokens": 15
  }
}
```
- **`choices: []` (empty array)** — critical difference from non-usage final chunk
- Usage chunk sent **after** normal final chunk (so you get 2 final chunks)
- If stream interrupted, usage chunk never arrives

---

### A.4 Error Handling in Stream

#### During Stream Completion
OpenAI **closes the connection** when an error occurs mid-stream (no error JSON sent). Clients see:
- `data: [DONE]` never arrives
- TCP connection closes
- Last received chunk may be incomplete

#### Mitigation
- Wrap stream reader in try-catch
- Detect incomplete message (missing `finish_reason`)
- Retry request if safe (idempotent)

---

### A.5 Non-Streaming Response (Baseline for Parity)

**Request:** `stream: false` (or omitted)

```json
{
  "id": "chatcmpl-123abc",
  "object": "chat.completion",
  "created": 1704067200,
  "model": "gpt-4o",
  "system_fingerprint": "fp_abc123",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello, how are you?"
      },
      "finish_reason": "stop",
      "logprobs": null
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "total_tokens": 15
  }
}
```

**Key Differences from Streaming:**
- `object: "chat.completion"` (not `.chunk`)
- `choices[].message` (not `.delta`)
- `message.role` + `message.content` together in one object
- `usage` always present (not separated into final chunk)
- Single response, no SSE format

---

## Part B: Responses API Streaming

### B.1 Event Wire Format

#### Response Headers
```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Transfer-Encoding: chunked
Connection: keep-alive
```

#### Event Format (SSE with Event Type)
```
event: response.created
data: {"event_id":"evt_1","type":"response.created","response":{...},"sequence_number":0}

event: response.output_item.added
data: {"event_id":"evt_2","type":"response.output_item.added",...,"sequence_number":1}

event: response.output_text.delta
data: {"event_id":"evt_3","type":"response.output_text.delta",...,"sequence_number":2}

event: response.completed
data: {"event_id":"evt_4","type":"response.completed",...,"sequence_number":3}
```

**Format Notes:**
- Both `event: <name>` line (for routing) AND `data: <json>` line present
- `event:` line determines how to dispatch the message
- `data:` JSON always includes `"type": <same-as-event>`
- Events have `sequence_number` for ordering guarantees
- **No `[DONE]` terminator** — stream ends when socket closes after last event

---

### B.2 Event Taxonomy (53+ Events)

Events organized by lifecycle phase & function:

#### B.2.1 Lifecycle Events

| Event Type | Description | Payload Highlights |
|---|---|---|
| `response.queued` | Response queued (if used with background tasks) | `response` with `status: "queued"` |
| `response.created` | Response object created, streaming begins | `response` object with initial state |
| `response.in_progress` | Response is processing | `response` object |
| `response.completed` | Response finished successfully | Full `response` + `usage` object |
| `response.incomplete` | Response stopped but not failed (e.g., interrupted) | `response` with `status: "incomplete"` |
| `response.failed` | Response failed with error | `response` + error details |
| `response.cancelled` | User cancelled the request | `response` with `status: "cancelled"` |

#### B.2.2 Output Item Events

| Event Type | Description | Fires When |
|---|---|---|
| `response.output_item.added` | New output item created | Message, reasoning, tool call added to response |
| `response.output_item.done` | Output item fully streamed | Item finished (final delta received) |

#### B.2.3 Content & Text Streaming

| Event Type | Fires On | Payload Fields |
|---|---|---|
| `response.content_part.added` | New content part in message | `content_part` object with `type`, `text` (or placeholder) |
| `response.content_part.done` | Content part finalized | Final `content_part` object with completed text |
| `response.output_text.delta` | Text token emitted | `delta` (text chunk), `sequence_number`, `content_index` |
| `response.output_text.done` | Text generation stopped | Final aggregated text |

#### B.2.4 Reasoning Events (O-Series Models)

| Event Type | Applies To | Payload |
|---|---|---|
| `response.reasoning_summary_part.added` | GPT-o1-like reasoning | Reasoning summary content part created |
| `response.reasoning_summary_part.done` | GPT-o1-like reasoning | Reasoning summary completed |
| `response.reasoning_summary_text.delta` | GPT-o1-like reasoning | Streaming reasoning summary text delta |
| `response.reasoning_summary_text.done` | GPT-o1-like reasoning | Reasoning summary text finalized |
| `response.reasoning.delta` | GPT-o1-like reasoning | Raw reasoning token delta (if exposed) |
| `response.reasoning.done` | GPT-o1-like reasoning | Reasoning finalized |

#### B.2.5 Tool-Related Events

**Function Calling:**
| Event | Fires When | Payload |
|---|---|---|
| `response.function_call_arguments.delta` | Function argument JSON streamed | `delta` (partial JSON string), `sequence_number` |
| `response.function_call_arguments.done` | Function argument fully streamed | `arguments` (complete JSON string), `name` |

**Web Search:**
| Event | Status Transition | Payload |
|---|---|---|
| `response.web_search_call.in_progress` | Web search started | `output_index`, `item_id`, `call_id` |
| `response.web_search_call.searching` | Web search active | Status update |
| `response.web_search_call.completed` | Web search finished | Results included |

**File Search:**
| Event | When | Payload |
|---|---|---|
| `response.file_search_call.in_progress` | File search started | `query`, `status` |
| `response.file_search_call.completed` | File search finished | Results array |

**Code Interpreter:**
| Event | When | Payload |
|---|---|---|
| `response.code_interpreter_call.in_progress` | Code execution started | Code snippet |
| `response.code_interpreter_call.interpreting` | Code actively interpreting | Code context |
| `response.code_interpreter_call.completed` | Code finished executing | Output (stdout/stderr/images) |

#### B.2.6 Refusal & Error Events

| Event | Meaning | Payload |
|---|---|---|
| `response.refusal.delta` | Refusal text emitted token-by-token | `delta` (text chunk), `sequence_number` |
| `response.refusal.done` | Refusal generation stopped | Final refusal text |
| `error` | Stream error (not response-level failure) | `code`, `message`, `param`, `sequence_number` |

---

### B.3 Detailed Event Payloads

#### B.3.1 response.created
```json
{
  "event_id": "evt_abc123",
  "type": "response.created",
  "response": {
    "id": "resp_123",
    "object": "response",
    "status": "in_progress",
    "model": "gpt-4o",
    "created_at": "2026-04-27T10:30:00Z",
    "output": [],
    "usage": null,
    "metadata": {
      "user_id": "user_123",
      "conversation_id": "conv_456"
    }
  },
  "sequence_number": 0
}
```

**Key Fields:**
- `response.status: "in_progress"` — always this on creation
- `response.output: []` — empty initially
- `response.usage: null` — filled on completion

---

#### B.3.2 response.output_item.added
```json
{
  "event_id": "evt_abc124",
  "type": "response.output_item.added",
  "output_item": {
    "id": "item_abc",
    "type": "message",
    "status": "in_progress",
    "content": []
  },
  "output_index": 0,
  "sequence_number": 1
}
```

**Item Types:**
- `"message"` — text/refusal output
- `"reasoning"` — reasoning summary (o-series)
- `"tool_call"` — function/web_search/file_search/code_interpreter call

---

#### B.3.3 response.content_part.added
```json
{
  "event_id": "evt_abc125",
  "type": "response.content_part.added",
  "content_part": {
    "type": "text",
    "text": ""
  },
  "output_index": 0,
  "content_index": 0,
  "item_id": "item_abc",
  "sequence_number": 2
}
```

**Content Part Types:**
- `"text"` — plain text response
- `"reasoning"` — reasoning summary text
- `"refusal"` — refusal message

---

#### B.3.4 response.output_text.delta
```json
{
  "event_id": "evt_abc126",
  "type": "response.output_text.delta",
  "item_id": "item_abc",
  "output_index": 0,
  "content_index": 0,
  "delta": "Hello ",
  "sequence_number": 3
}
```

**Rules:**
- Deltas must be appended in order by `sequence_number`
- Text is only complete when paired with `.done` event
- Do not infer completion from `delta` alone

---

#### B.3.5 response.output_text.done
```json
{
  "event_id": "evt_abc127",
  "type": "response.output_text.done",
  "item_id": "item_abc",
  "output_index": 0,
  "content_index": 0,
  "text": "Hello there, how can I help?",
  "sequence_number": 4
}
```

**Guarantees:**
- `text` field contains the fully assembled text
- All deltas have been received
- Next event or `response.completed` follows

---

#### B.3.6 response.function_call_arguments.delta
```json
{
  "event_id": "evt_abc128",
  "type": "response.function_call_arguments.delta",
  "item_id": "item_abc",
  "output_index": 0,
  "delta": "{\"location\": \"S",
  "sequence_number": 5
}
```

**Note:**
- `delta` is raw JSON string (may be partial JSON)
- Buffer deltas until `.done` event arrives

---

#### B.3.7 response.function_call_arguments.done
```json
{
  "event_id": "evt_abc129",
  "type": "response.function_call_arguments.done",
  "item_id": "item_abc",
  "output_index": 0,
  "name": "get_weather",
  "arguments": "{\"location\": \"San Francisco\"}",
  "sequence_number": 6
}
```

**Guarantees:**
- `arguments` is complete, valid JSON
- `name` is the function name
- Ready to parse and execute

---

#### B.3.8 response.reasoning_summary_text.delta
```json
{
  "event_id": "evt_abc130",
  "type": "response.reasoning_summary_text.delta",
  "item_id": "item_abc",
  "output_index": 0,
  "summary_index": 0,
  "delta": "Let me think ",
  "sequence_number": 7
}
```

**For O-Series Models:**
- Emitted during extended reasoning
- `summary_index` tracks which summary (if multiple)

---

#### B.3.9 response.web_search_call.in_progress
```json
{
  "event_id": "evt_abc131",
  "type": "response.web_search_call.in_progress",
  "item_id": "item_abc",
  "output_index": 0,
  "call_id": "call_web_123",
  "status": "in_progress",
  "sequence_number": 8
}
```

---

#### B.3.10 response.web_search_call.completed
```json
{
  "event_id": "evt_abc132",
  "type": "response.web_search_call.completed",
  "item_id": "item_abc",
  "output_index": 0,
  "call_id": "call_web_123",
  "results": [
    {
      "url": "https://example.com",
      "title": "Example",
      "snippet": "...",
      "last_updated": "2026-04-27"
    }
  ],
  "sequence_number": 9
}
```

---

#### B.3.11 response.code_interpreter_call.completed
```json
{
  "event_id": "evt_abc133",
  "type": "response.code_interpreter_call.completed",
  "item_id": "item_abc",
  "output_index": 0,
  "call_id": "call_code_456",
  "code": "print('hello')",
  "output": [
    {
      "type": "text",
      "text": "hello"
    },
    {
      "type": "image",
      "base64": "iVBORw0KGgoAAAANS..."
    }
  ],
  "sequence_number": 10
}
```

---

#### B.3.12 response.completed
```json
{
  "event_id": "evt_abc134",
  "type": "response.completed",
  "response": {
    "id": "resp_123",
    "object": "response",
    "status": "completed",
    "model": "gpt-4o",
    "created_at": "2026-04-27T10:30:00Z",
    "output": [
      {
        "id": "item_abc",
        "type": "message",
        "content": [
          {
            "type": "text",
            "text": "Hello there, how can I help?"
          }
        ]
      }
    ],
    "usage": {
      "input_tokens": 50,
      "output_tokens": 25,
      "total_tokens": 75,
      "output_tokens_details": {
        "reasoning_tokens": 0
      }
    },
    "metadata": {}
  },
  "sequence_number": 11
}
```

**Usage Object:**
| Field | Type | Notes |
|-------|------|-------|
| `input_tokens` | int | Total input tokens (including cache) |
| `output_tokens` | int | Total output tokens |
| `total_tokens` | int | Sum of input + output |
| `output_tokens_details` | object | Contains `reasoning_tokens` (for o-series) |

---

#### B.3.13 error Event
```json
{
  "event_id": "evt_abc135",
  "type": "error",
  "code": "server_error",
  "message": "Internal server error",
  "param": null,
  "sequence_number": 12
}
```

**Error Codes (Common):**
- `"server_error"` — 5xx backend issue
- `"rate_limit_exceeded"` — Quota exhausted
- `"invalid_prompt"` — Prompt validation failed
- `"timeout"` — Request timed out
- `"invalid_request"` — Malformed request

**Handling:**
- Do NOT retry automatically on `server_error` (may be permanent)
- Retry with backoff on `rate_limit_exceeded`
- Log `param` if present (indicates which field failed)

---

### B.4 Non-Streaming Response Object

**Request:** `stream: false` (or omitted)

```json
{
  "id": "resp_123",
  "object": "response",
  "status": "completed",
  "model": "gpt-4o",
  "created_at": "2026-04-27T10:30:00Z",
  "output": [
    {
      "id": "item_msg_abc",
      "type": "message",
      "status": "completed",
      "content": [
        {
          "type": "text",
          "text": "Hello, how can I help you today?",
          "annotations": [
            {
              "type": "citation",
              "text": "citation text",
              "file_id": "file_abc",
              "file_name": "document.pdf"
            }
          ]
        }
      ]
    },
    {
      "id": "item_tool_abc",
      "type": "tool_call",
      "status": "completed",
      "call_id": "call_func_123",
      "name": "get_weather",
      "arguments": "{\"location\": \"SF\"}"
    }
  ],
  "usage": {
    "input_tokens": 50,
    "output_tokens": 25,
    "total_tokens": 75,
    "output_tokens_details": {
      "reasoning_tokens": 0
    }
  },
  "metadata": {
    "user_id": "user_123"
  }
}
```

**Output Item Types:**
- `"message"` → `content` is array of text/refusal/annotation objects
- `"reasoning"` → `content` is array of reasoning parts
- `"tool_call"` → `name` + `arguments` fields (JSON string)

**Content Types in Message:**
- `"text"` → `text` field (with optional `annotations`)
- `"refusal"` → `refusal` field
- `"output_text"` → `text` field (legacy)

---

## Part C: Implementation Checklist

### Chat Completions Streaming
- [ ] Parse data-only SSE (no `event:` line)
- [ ] Handle `[DONE]` sentinel (not JSON)
- [ ] Buffer tool_calls by `index` across chunks
- [ ] Detect usage chunk: `choices: []` + `usage != null`
- [ ] Close stream on missing `finish_reason` (error case)
- [ ] Re-emit non-streaming response structure for comparison

### Responses API Streaming
- [ ] Parse `event: <name>` + `data: <json>` SSE
- [ ] Route by `type` field (matches `event:` name)
- [ ] Buffer text deltas by `content_index` until `.done`
- [ ] Buffer function args by `output_index` until `.done`
- [ ] Track `sequence_number` for ordering (optional; events should arrive in order)
- [ ] Emit `usage` only on `response.completed` (not per-event)
- [ ] Handle `error` events separately from `response.failed`

---

## Unresolved Questions

1. **Tool Call Indexing:** If same tool is called twice in one response, do tool_calls deltas have unique `id` fields for each call? (Assumption: yes, via unique `call_*` IDs)

2. **Reasoning Tokens:** Are `reasoning_tokens` present in `output_tokens_details` for all models, or only o-series? (Assumption: present but `0` for non-reasoning models)

3. **Content Part Annotations:** In Responses API non-streaming, do `content_part.added` events include empty `annotations: []` or is it omitted? (Could affect parsing)

4. **Web Search Pagination:** If web search returns 100+ results, are they all in one `.completed` event or paginated? (Not documented)

5. **Code Interpreter Images:** Are base64 images split into multiple events or single complete event? (Not specified)

6. **Cache Headers:** Do Chat Completions responses include `x-openai-cache-*` headers? (Assumed not from search results, but unconfirmed)

7. **Reason Phrase:** What is the exact HTTP reason phrase on streaming responses (e.g., `200 OK` vs bare `200`)? (Not specified in docs)

---

## References

### Primary Sources
- [Streaming API responses | OpenAI Developers](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Chat Completions streaming | OpenAI API Reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)
- [Responses API streaming | OpenAI API Reference](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [Responses API: Simple Events Guide | OpenAI Developer Community](https://community.openai.com/t/responses-api-streaming-the-simple-guide-to-events/1363122)

### Secondary Sources
- [openai-python ChatCompletionChunk types](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_chunk.py)
- [OpenAI Cookbook: How to stream completions](https://cookbook.openai.com/examples/how_to_stream_completions)
- [OpenAI Cookbook: Responses API with reasoning](https://developers.openai.com/cookbook/examples/responses_api/reasoning_items)

---

**Document Status:** Complete for API v1 (as of 2026-04-27)  
**Next:** Implement wire-format handlers in server code; validate against real OpenAI SDK payloads
