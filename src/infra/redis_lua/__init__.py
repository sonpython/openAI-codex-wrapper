"""
Lua script loader for Redis atomic rate-limit operations.

Usage:
    from src.infra.redis_lua import load_script
    sw = load_script(redis_client, "sliding-window")
    result = await sw(keys=[key], args=[now_ms, window_ms, limit, entry_id])

Scripts are loaded from .lua files in this package directory.
redis-py compiles each script via SCRIPT LOAD on first use (EVALSHA fast-path
for subsequent calls — single round-trip, no re-transmission of script body).

Available scripts:
  sliding-window   — RPM sliding-window via ZSET (see sliding-window.lua)
  tpm_check        — TPM per-window counter via INCRBYFLOAT (see tpm_check.lua)
  concurrent_check — Concurrent counter with PEXPIRE refresh (see concurrent_check.lua)
  edge_ip_check    — Pre-auth per-IP bucket (see edge_ip_check.lua)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from redis.asyncio import Redis

_SCRIPT_DIR = Path(__file__).parent


def load_script(redis: Redis[Any], name: str) -> Any:  # returns redis Script object
    """Load a Lua script by name (without .lua extension) and register it.

    The returned object is callable:
        result = await script(keys=[...], args=[...])

    redis-py's register_script() returns a Script object that uses EVALSHA
    on subsequent calls (single SHA per script body — O(1) Redis lookup).
    Falls back to EVAL if the SHA is evicted (unlikely on prod Redis).

    Args:
        redis: Async Redis client instance.
        name:  Script filename without .lua suffix (e.g. "sliding-window").

    Returns:
        A callable redis-py Script object.

    Raises:
        FileNotFoundError: if the .lua file does not exist.
    """
    path = _SCRIPT_DIR / f"{name}.lua"
    source = path.read_text(encoding="utf-8")
    return redis.register_script(source)
