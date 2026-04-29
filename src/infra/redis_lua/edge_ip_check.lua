-- edge_ip_check.lua — Pre-auth per-IP rate limit bucket.
--
-- Increments a per-IP counter and rejects if over the limit.
-- Used by EdgeIPLimiter before AuthMiddleware runs, preventing
-- argon2-burn DoS amplification (red team C2 fix).
--
-- KEYS[1] = "ip_pre_auth:{ip}"
-- ARGV[1] = limit   (IP_PRE_AUTH_RPM; default 30)
-- ARGV[2] = ttl_ms  (window TTL in milliseconds; default 60000)
--
-- Returns: 0 on reject (over limit), >=1 (new counter value) on accept.

local key   = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl   = tonumber(ARGV[2])

local v = redis.call('INCR', key)

-- Set TTL only on first increment so the window is fixed from first bad request.
-- NX flag: only set if not already set (preserves existing window).
redis.call('PEXPIRE', key, ttl, 'NX')

if v > limit then
    return 0
end

return v
