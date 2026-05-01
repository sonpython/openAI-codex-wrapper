"""
CRUD helpers for the plans table.

Provides an in-process TTL cache so the hot request path never hits Postgres
for tier lookups. Plans change rarely (admin operation); 5-min staleness is
acceptable and documented in the phase-06 risk table.

Usage:
    limits = await get_limits(session, "free")
    # {"rpm": 20, "tpm": 20000, "concurrent": 2, "monthly_tokens": 100000}
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Plan
from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Module-level cache: tier -> (limits_dict, expires_at_monotonic)
# Dict values intentionally typed as Any to avoid importing Plan everywhere.
_CACHE: dict[str, tuple[dict[str, int], float]] = {}

# Fallback limits used when DB is unreachable and cache is cold.
# Fail-open: use conservative free-tier limits rather than blocking all traffic.
_FALLBACK_LIMITS: dict[str, dict[str, int]] = {
    "free": {"rpm": 20, "tpm": 20000, "concurrent": 2, "monthly_tokens": 100_000},
    "pro": {"rpm": 200, "tpm": 200_000, "concurrent": 10, "monthly_tokens": 2_000_000},
    "enterprise": {
        "rpm": 2000,
        "tpm": 2_000_000,
        "concurrent": 50,
        "monthly_tokens": 20_000_000,
    },
}


def _plan_to_dict(plan: Plan) -> dict[str, int]:
    return {
        "rpm": plan.rpm,
        "tpm": plan.tpm,
        "concurrent": plan.concurrent,
        "monthly_tokens": plan.monthly_tokens,
    }


async def get_limits(session: AsyncSession, tier: str) -> dict[str, int]:
    """Return rate-limit values for the given tier.

    Checks in-process cache first (TTL = tier_cache_ttl_seconds, default 300s).
    On cache miss or expiry, reloads ALL tiers from Postgres in one query and
    repopulates the cache — so the first request after expiry warms the full cache.

    On DB error: logs WARN and returns fallback limits (fail-open).

    Args:
        session: SQLAlchemy async session (main pool).
        tier:    Tier string — "free" | "pro" | "enterprise".

    Returns:
        Dict with keys: rpm, tpm, concurrent, monthly_tokens.
    """
    settings = get_settings()
    ttl = float(settings.tier_cache_ttl_seconds)
    now = time.monotonic()

    cached = _CACHE.get(tier)
    if cached is not None and cached[1] > now:
        return cached[0]

    # Cache miss or expired — reload all tiers in one round-trip.
    try:
        result = await session.execute(select(Plan))
        plans: list[Plan] = list(result.scalars().all())
        expires_at = now + ttl
        for plan in plans:
            _CACHE[plan.tier] = (_plan_to_dict(plan), expires_at)
        if tier in _CACHE:
            return _CACHE[tier][0]
        # Tier not in DB — log and return conservative fallback.
        logger.warning("plans.tier_not_found", tier=tier)
    except Exception:  # noqa: BLE001
        logger.warning("plans.db_error_using_fallback", tier=tier, exc_info=True)

    return _FALLBACK_LIMITS.get(tier, _FALLBACK_LIMITS["free"])


def invalidate_cache() -> None:
    """Clear the in-process tier cache.  Useful in tests and after plan updates."""
    _CACHE.clear()


async def update(
    session: AsyncSession,
    tier: str,
    rpm: int,
    tpm: int,
    concurrent: int,
    monthly_quota: int,
) -> Plan:
    """Upsert rate-limit values for a given tier.

    Uses INSERT … ON CONFLICT (tier) DO UPDATE so this is safe whether or not
    the tier row already exists.  Values are validated (>= 0) by the caller
    (Pydantic schema) before this function is invoked.

    Args:
        session:       SQLAlchemy async session.
        tier:          Tier string — "free" | "pro" | "ent" | "enterprise".
        rpm:           Requests per minute (>= 0).
        tpm:           Tokens per minute (>= 0).
        concurrent:    Max concurrent requests (>= 0).
        monthly_quota: Monthly token quota (>= 0); maps to monthly_tokens column.

    Returns:
        The updated Plan ORM row (re-fetched after upsert).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    stmt = (
        pg_insert(Plan)
        .values(
            tier=tier,
            rpm=rpm,
            tpm=tpm,
            concurrent=concurrent,
            monthly_tokens=monthly_quota,
        )
        .on_conflict_do_update(
            index_elements=["tier"],
            set_=dict(
                rpm=rpm,
                tpm=tpm,
                concurrent=concurrent,
                monthly_tokens=monthly_quota,
            ),
        )
    )
    await session.execute(stmt)
    await session.flush()

    # Re-fetch to get the authoritative DB state (including server_defaults).
    result = await session.execute(select(Plan).where(Plan.tier == tier))
    updated = result.scalar_one()
    return updated


async def list_all(session: AsyncSession) -> list[Plan]:
    """Return all Plan rows ordered by tier name."""
    result = await session.execute(select(Plan).order_by(Plan.tier))
    return list(result.scalars().all())
