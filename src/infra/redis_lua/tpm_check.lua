-- tpm_check.lua — TPM per-window counter via INCRBYFLOAT.
--
-- Replaces the broken negative-cost ZSET approach (red team C5).
-- Uses a single bucket key per 60s window so INCRBYFLOAT semantics are
-- total-only and the arithmetic is always sound.
--
-- KEYS[1] = "rl:tpm:{key_id}:{window_id}"   window_id = floor(now_ms / window_ms)
-- ARGV[1] = now_ms      (current epoch milliseconds as string)
-- ARGV[2] = window_ms   (window size; default 60000)
-- ARGV[3] = limit       (tier TPM)
-- ARGV[4] = cost        (estimate; positive float tokens)
--
-- Returns: {allowed, current, remaining, reset_ms, limit}
--   allowed  = 1 (request allowed) | 0 (rejected)
--   current  = token count in window AFTER this call (or before if rejected)
--   remaining = max(0, limit - current)
--   reset_ms = ms until the window boundary resets
--   limit    = echo the limit back

local key       = KEYS[1]
local now_ms    = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit     = tonumber(ARGV[3])
local cost      = tonumber(ARGV[4])

-- Derive the window boundary from the key's window_id embedded by caller.
-- window_id = floor(now_ms / window_ms), so next boundary is at:
--   (window_id + 1) * window_ms
-- We extract it from now_ms directly:
local window_id  = math.floor(now_ms / window_ms)
local next_edge  = (window_id + 1) * window_ms
local reset_ms   = next_edge - now_ms

-- 1. Read current count (treat missing key as 0).
local raw     = redis.call('GET', key)
local current = tonumber(raw or '0')

-- 2. Reject if adding cost would exceed the limit.
if current + cost > limit then
    local remaining = math.max(0, limit - current)
    return {0, current, remaining, reset_ms, limit}
end

-- 3. Increment and set a 2× window TTL (covers prior-window key during transition).
local new_val = redis.call('INCRBYFLOAT', key, cost)
redis.call('PEXPIRE', key, window_ms * 2)

local new_num   = tonumber(new_val)
local remaining = math.max(0, limit - new_num)
return {1, new_num, remaining, reset_ms, limit}
