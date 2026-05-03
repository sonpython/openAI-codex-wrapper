"""
CRUD helpers for the api_keys table.

Includes the _BG_TASKS fire-and-forget pattern (Red Team C8 fix):
  - Module-level _BG_TASKS set holds strong references to in-flight asyncio
    Tasks, preventing GC from killing them mid-execution.
  - update_last_used_fire_and_forget() uses the SEPARATE bg_session() pool
    (pool_size=3, pool_timeout=0.5s). On pool-acquire timeout the write is
    DROPPED with a WARN log — last_used_at is best-effort; staleness is fine.
  - Never uses the main request pool for background writes (avoids C9 pool
    exhaustion under burst load).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.hashing import generate_api_key, verify_key
from src.db.engine import bg_session
from src.db.models import ApiKey

logger = structlog.get_logger(__name__)

# Execution mode constants — single source of truth; imported by admin REST + UI routes.
VALID_MODES: frozenset[str] = frozenset({"sandbox", "vps", "local-bridge"})
DEFAULT_MODE: str = "sandbox"

# Strong references to in-flight background tasks.
# Without this set, the GC may collect a Task before it completes if no other
# reference exists (asyncio only holds a weak ref). The done_callback removes
# each task once it finishes, keeping the set bounded.
_BG_TASKS: set[asyncio.Task[None]] = set()


async def create(
    session: AsyncSession,
    user_id: UUID,
    name: str,
    tier: str = "free",
    mode: str = DEFAULT_MODE,
) -> tuple[ApiKey, str]:
    """Create a new ApiKey row and return (api_key_row, plaintext).

    Plaintext is returned ONCE here. Callers must forward it to the admin
    response — it is never retrievable again after this call returns.

    Raises ValueError if mode is not in VALID_MODES.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        user_id=user_id,
        key_hash=key_hash,
        prefix=prefix,
        name=name,
        tier=tier,
        mode=mode,
    )
    session.add(api_key)
    await session.flush()  # populate id + server_default created_at
    return api_key, plaintext


async def get_active_by_prefix_and_verify(
    session: AsyncSession,
    plaintext: str,
) -> ApiKey | None:
    """Look up an active (non-revoked) ApiKey matching the given plaintext.

    Strategy:
      1. Extract prefix (first 12 chars) — cheap indexed SELECT capped at 2 rows.
      2. For each candidate row: argon2 verify offloaded to thread pool so the
         event loop is not blocked during the ~25-50 ms CPU-intensive verify.
      3. Return the first match, or None if none verify.

    Prefix collision space is ~2^72 (12 b64url chars); per-prefix cardinality
    stays ≈ 1 in practice. The LIMIT 2 cap is defense-in-depth against
    attacker-controlled prefix collisions that would otherwise amplify CPU usage
    (each argon2 verify holds 64 MiB for ~25 ms).
    """
    prefix = plaintext[:12]
    result = await session.execute(
        select(ApiKey)
        .where(
            ApiKey.prefix == prefix,
            ApiKey.revoked_at.is_(None),
        )
        .limit(2)  # cap: prefix collision is a config error, not a feature
    )
    candidates = result.scalars().all()

    for candidate in candidates:
        # Offload CPU-intensive argon2 verify to thread pool.
        # argon2-cffi releases the GIL during the C call, so this is genuinely
        # concurrent and prevents blocking all other async work on this worker.
        match = await asyncio.to_thread(verify_key, plaintext, candidate.key_hash)
        if match:
            return candidate

    return None


async def get_by_id(session: AsyncSession, key_id: UUID) -> ApiKey | None:
    """Return an ApiKey by primary key, or None."""
    result = await session.execute(select(ApiKey).where(ApiKey.id == key_id))
    return result.scalar_one_or_none()


async def revoke(session: AsyncSession, key_id: UUID) -> bool:
    """Soft-delete an ApiKey by setting revoked_at = now().

    Returns True if a row was updated, False if the key_id was not found.
    """
    from sqlalchemy import func  # noqa: PLC0415

    result = await session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=func.now()).returning(ApiKey.id)
    )
    updated = result.scalar_one_or_none()
    return updated is not None


async def _do_update_last_used(key_id: UUID) -> None:
    """Background coroutine: update last_used_at via the bg pool.

    Uses bg_session() (pool_size=3, pool_timeout=0.5s). If the pool is
    saturated, TimeoutError is raised by SQLAlchemy — we catch, log WARN,
    and drop the update. This is intentional: last_used_at is best-effort.
    Never blocks the request path.
    """
    from sqlalchemy import func  # noqa: PLC0415

    try:
        async with bg_session() as s:
            await s.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=func.now())
            )
            await s.commit()
    except TimeoutError:
        logger.warning("auth.last_used.pool_timeout", key_id=str(key_id))
    except Exception:
        logger.warning("auth.last_used.bg_failed", key_id=str(key_id), exc_info=True)


def update_last_used_fire_and_forget(key_id: UUID) -> None:
    """Schedule a best-effort last_used_at update without blocking the caller.

    Task is held in _BG_TASKS until completion to prevent GC mid-execution.
    The done_callback removes the strong reference once the task finishes.
    """
    task: asyncio.Task[None] = asyncio.create_task(_do_update_last_used(key_id))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
