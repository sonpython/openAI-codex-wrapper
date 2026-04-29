-- sliding-window.lua — RPM atomic sliding-window check via ZSET.
--
-- KEYS[1] = "rl:rpm:{key_id}"
-- ARGV[1] = now_ms          (current epoch milliseconds as string)
-- ARGV[2] = window_ms       (window size; default 60000)
-- ARGV[3] = limit           (tier RPM)
-- ARGV[4] = entry_id        (unique string — avoids score collisions)
--
-- Returns: {allowed, current, remaining, reset_ms, limit}
--   allowed  = 1 (request allowed) | 0 (rejected)
--   current  = current count inside the window AFTER this call
--   remaining = max(0, limit - current)
--   reset_ms = ms until the window resets (TTL of oldest entry)
--   limit    = echo the limit back to the caller

local key        = KEYS[1]
local now_ms     = tonumber(ARGV[1])
local window_ms  = tonumber(ARGV[2])
local limit      = tonumber(ARGV[3])
local entry_id   = ARGV[4]

-- 1. Evict entries that have fallen outside the sliding window.
redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)

-- 2. Count remaining entries in the current window.
local count = redis.call('ZCARD', key)

-- 3. Reject if adding this request would exceed the limit.
if count + 1 > limit then
    local oldest_score = 0
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    if #oldest >= 2 then
        oldest_score = tonumber(oldest[2])
    else
        oldest_score = now_ms - window_ms
    end
    local reset_ms = math.max(0, (oldest_score + window_ms) - now_ms)
    local remaining = math.max(0, limit - count)
    return {0, count, remaining, reset_ms, limit}
end

-- 4. Add the new entry and refresh the key TTL.
redis.call('ZADD', key, now_ms, entry_id)
redis.call('PEXPIRE', key, window_ms)

local new_count = count + 1
local remaining = math.max(0, limit - new_count)
return {1, new_count, remaining, window_ms, limit}
