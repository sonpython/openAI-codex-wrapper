-- concurrent_check.lua — Atomic concurrent-request counter.
--
-- Red-team C4 fix: PEXPIRE is refreshed on EVERY call (not only on first set)
-- so active long-running keys never lose their TTL mid-request.
--
-- KEYS[1] = "rl:concurrent:{key_id}"
-- ARGV[1] = limit   (tier concurrent cap)
-- ARGV[2] = ttl_ms  (TTL in milliseconds; default 60000)
--
-- Returns: 0 on reject (over cap), >=1 (the new counter value) on accept.

local key   = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl   = tonumber(ARGV[2])

-- INCR first, then check — this is the atomic pattern that prevents TOCTOU.
local v = redis.call('INCR', key)

-- ALWAYS refresh TTL — critical for streams > 60s (C4 fix).
redis.call('PEXPIRE', key, ttl)

if v > limit then
    -- Roll back the INCR we just did; key stays at previous value.
    redis.call('DECR', key)
    return 0
end

return v
