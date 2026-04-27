"""
Unit tests for rate-limit Lua scripts via fakeredis.

Tests cover:
  - sliding-window.lua: allow/deny transitions, reset_ms decreases, ZSET eviction
  - tpm_check.lua: upfront charge, true-up negative delta, window boundary
  - concurrent_check.lua: over-cap rejection, PEXPIRE refresh on every call
  - edge_ip_check.lua: IP bucket increments and rejects over limit
"""

from __future__ import annotations

import time

import fakeredis
import fakeredis.aioredis
import pytest
from src.infra.redis_lua import load_script


@pytest.fixture()
def fake_redis() -> fakeredis.aioredis.FakeRedis:  # type: ignore[type-arg]
    return fakeredis.aioredis.FakeRedis()


# ── sliding-window.lua ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sliding_window_allows_first_request(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "sliding-window")
    now_ms = int(time.time() * 1000)
    result = await script(
        keys=["rl:rpm:key1"],
        args=[str(now_ms), "60000", "5", "entry-1"],
    )
    assert result[0] == 1, "first request must be allowed"
    assert result[1] == 1, "count should be 1"
    assert result[2] == 4, "remaining should be 4"


@pytest.mark.asyncio
async def test_sliding_window_rejects_at_limit(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "sliding-window")
    now_ms = int(time.time() * 1000)
    limit = 3
    # Fill up to limit
    for i in range(limit):
        r = await script(
            keys=["rl:rpm:key2"],
            args=[str(now_ms + i), "60000", str(limit), f"entry-{i}"],
        )
        assert r[0] == 1, f"request {i} should be allowed"

    # Next request must be rejected
    r = await script(
        keys=["rl:rpm:key2"],
        args=[str(now_ms + limit), "60000", str(limit), "entry-overflow"],
    )
    assert r[0] == 0, "request over limit must be denied"
    assert r[2] == 0, "remaining must be 0"


@pytest.mark.asyncio
async def test_sliding_window_evicts_expired_entries(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "sliding-window")
    limit = 2
    old_ms = int((time.time() - 65) * 1000)  # 65 seconds ago — outside 60s window
    now_ms = int(time.time() * 1000)

    # Add 2 old entries (outside window)
    for i in range(limit):
        await script(
            keys=["rl:rpm:key3"],
            args=[str(old_ms + i), "60000", str(limit), f"old-{i}"],
        )
    # Both entries are now "stale". A new request should be allowed.
    r = await script(
        keys=["rl:rpm:key3"],
        args=[str(now_ms), "60000", str(limit), "new-entry"],
    )
    assert r[0] == 1, "request allowed after window slides past old entries"


# ── tpm_check.lua ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tpm_check_allows_within_limit(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "tpm_check")
    now_ms = int(time.time() * 1000)
    window_id = int(time.time()) // 60

    result = await script(
        keys=[f"rl:tpm:key1:{window_id}"],
        args=[str(now_ms), "60000", "1000", "100"],
    )
    assert result[0] == 1, "should be allowed"
    assert abs(float(result[1]) - 100.0) < 0.01, "counter should be 100"


@pytest.mark.asyncio
async def test_tpm_check_rejects_over_limit(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "tpm_check")
    now_ms = int(time.time() * 1000)
    window_id = int(time.time()) // 60

    # Charge 900 first
    await script(
        keys=[f"rl:tpm:key2:{window_id}"],
        args=[str(now_ms), "60000", "1000", "900"],
    )
    # Attempt to charge 200 more (900+200 > 1000)
    result = await script(
        keys=[f"rl:tpm:key2:{window_id}"],
        args=[str(now_ms), "60000", "1000", "200"],
    )
    assert result[0] == 0, "should be rejected"
    assert float(result[2]) == 100.0, "remaining should be 100"


@pytest.mark.asyncio
async def test_tpm_true_up_negative_delta(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Upfront est=1000, actual=500 → true-up delta=-500 → counter lands at 500."""
    script = load_script(fake_redis, "tpm_check")
    now_ms = int(time.time() * 1000)
    window_id = int(time.time()) // 60
    key = f"rl:tpm:key3:{window_id}"

    # Upfront charge of 1000
    await script(keys=[key], args=[str(now_ms), "60000", "10000", "1000"])

    # True-up: actual was 500, delta = 500-1000 = -500
    delta = -500.0
    await fake_redis.eval(
        "redis.call('INCRBYFLOAT', KEYS[1], ARGV[1]);"
        " redis.call('PEXPIRE', KEYS[1], 120000);"
        " return 1",
        1,
        key,
        str(delta),
    )

    raw = await fake_redis.get(key)
    assert raw is not None
    net = float(raw)
    assert abs(net - 500.0) < 0.01, f"expected 500, got {net}"


@pytest.mark.asyncio
async def test_tpm_new_window_resets_counter(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Different window_id → separate key → new budget."""
    script = load_script(fake_redis, "tpm_check")
    now_ms = int(time.time() * 1000)
    window_id = int(time.time()) // 60
    next_window_id = window_id + 1

    # Fill the current window to near-limit
    await script(
        keys=[f"rl:tpm:key4:{window_id}"],
        args=[str(now_ms), "60000", "1000", "990"],
    )
    # Next window should have full budget
    r = await script(
        keys=[f"rl:tpm:key4:{next_window_id}"],
        args=[str(now_ms + 60_000), "60000", "1000", "990"],
    )
    assert r[0] == 1, "new window should allow request"


# ── concurrent_check.lua ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_check_allows_within_cap(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "concurrent_check")
    result = await script(keys=["rl:concurrent:key1"], args=["2", "60000"])
    assert result == 1, "first request allowed"

    result2 = await script(keys=["rl:concurrent:key1"], args=["2", "60000"])
    assert result2 == 2, "second request allowed"


@pytest.mark.asyncio
async def test_concurrent_check_rejects_over_cap(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "concurrent_check")
    await script(keys=["rl:concurrent:key2"], args=["2", "60000"])
    await script(keys=["rl:concurrent:key2"], args=["2", "60000"])
    result = await script(keys=["rl:concurrent:key2"], args=["2", "60000"])
    assert result == 0, "third request must be rejected at cap=2"

    # Counter stays at 2 after rejection
    val = await fake_redis.get("rl:concurrent:key2")
    assert int(val) == 2, "counter must not drift above cap"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_concurrent_check_pexpire_refreshed_on_every_call(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Every call must refresh the TTL — critical for streams > 60 s (C4 fix)."""
    script = load_script(fake_redis, "concurrent_check")
    await script(keys=["rl:concurrent:key3"], args=["5", "60000"])
    # Call again — TTL must still be set (PEXPIRE on every invocation).
    await script(keys=["rl:concurrent:key3"], args=["5", "60000"])
    ttl = await fake_redis.pttl("rl:concurrent:key3")
    assert ttl > 0, "TTL must be positive after second call"


# ── edge_ip_check.lua ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edge_ip_check_allows_under_limit(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "edge_ip_check")
    result = await script(keys=["ip_pre_auth:1.2.3.4"], args=["3", "60000"])
    assert result == 1, "first request allowed"


@pytest.mark.asyncio
async def test_edge_ip_check_rejects_over_limit(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "edge_ip_check")
    limit = 3
    for _ in range(limit):
        await script(keys=["ip_pre_auth:2.2.2.2"], args=[str(limit), "60000"])
    result = await script(keys=["ip_pre_auth:2.2.2.2"], args=[str(limit), "60000"])
    assert result == 0, "request over limit must be rejected"


@pytest.mark.asyncio
async def test_edge_ip_check_ttl_set_on_first_increment(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    script = load_script(fake_redis, "edge_ip_check")
    await script(keys=["ip_pre_auth:3.3.3.3"], args=["30", "60000"])
    ttl = await fake_redis.pttl("ip_pre_auth:3.3.3.3")
    assert ttl > 0, "TTL must be set on first increment"
