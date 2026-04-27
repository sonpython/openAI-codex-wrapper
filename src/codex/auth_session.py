"""
Codex CLI auth session health monitoring.

Provides:
  verify_codex_session()        — one-shot probe; returns (ok, expires_at)
  start_poller(app)             — starts background polling task; returns Task

Wiring (gateway/app.py lifespan):
  startup:  app.state.codex_session_healthy = False
            app.state._codex_poll_task = await start_poller(app)
  shutdown: app.state._codex_poll_task.cancel(); await with suppress

Probe strategy (researcher-01 §5):
  1. Run ``codex auth status`` (or ``codex login status``) with 3s timeout.
     Exit 0 → healthy; non-zero → try fallback.
  2. Fallback: stat + parse ``<CODEX_AUTH_DIR>/auth.json``.
     Present + ``expires_at`` in future → healthy.
  3. Any error → (False, None) + WARNING log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from src.settings import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = structlog.get_logger(__name__)


async def _probe_cli(codex_bin: str, timeout: int) -> tuple[bool, datetime | None]:
    """Try ``codex auth status`` then ``codex login status``.

    Returns (True, None) on exit-0, (False, None) when both variants tried and
    neither returned 0.

    H-5 fix: both subcommand variants are always tried regardless of exit code.
    Rationale: "subcommand not found" may return exit 1, 2, or 127/128 depending
    on the shell and codex version. The only definitive signal for "auth OK" is
    exit 0. Any non-zero → try next variant before returning False.
    Shell convention: exit 127 = command not found (sh), exit 2 = argparse/usage
    error (Python CLIs), exit 128+N = killed by signal N. We do NOT gate on a
    specific code; we try both and trust exit-0 exclusively.
    """
    for subargs in (["auth", "status"], ["login", "status"]):
        try:
            proc = await asyncio.create_subprocess_exec(
                codex_bin,
                *subargs,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=float(timeout))
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                logger.warning("codex.auth.probe_timeout", subargs=subargs)
                # Timeout on one variant → try next variant
                continue

            if proc.returncode == 0:
                return True, None
            # Non-zero exit: subcommand may be unknown → fall through to next variant
            logger.debug(
                "codex.auth.probe_nonzero",
                subargs=subargs,
                exit_code=proc.returncode,
            )
        except FileNotFoundError:
            logger.warning("codex.auth.bin_not_found", codex_bin=codex_bin)
            return False, None
        except Exception:
            logger.warning("codex.auth.probe_error", subargs=subargs, exc_info=True)
            return False, None

    # Both variants tried; neither returned exit 0 → unhealthy
    return False, None


def _probe_auth_json(auth_dir: str) -> tuple[bool, datetime | None]:
    """Fallback: parse ``<auth_dir>/auth.json`` for ``expires_at``."""
    auth_path = Path(auth_dir) / "auth.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("codex.auth.json_missing", path=str(auth_path))
        return False, None
    except Exception:
        logger.warning("codex.auth.json_parse_error", path=str(auth_path), exc_info=True)
        return False, None

    raw_exp = data.get("expires_at")
    if not raw_exp:
        # H-4 fix: no expiry field → treat as unhealthy (fail-closed).
        # The CLI probe ALREADY failed before reaching here, meaning codex itself
        # reported the session as broken. Trusting a file without an expiry field
        # overrides codex's own verdict — incorrect. Require explicit expiry.
        logger.warning("codex.auth.json_no_expiry")
        return False, None

    try:
        expires_at = datetime.fromisoformat(str(raw_exp).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("codex.auth.json_bad_expiry", raw=str(raw_exp)[:80])
        # H-4 fix: unparseable expiry → unhealthy (fail-closed), not assume valid.
        return False, None

    now = datetime.now(tz=UTC)
    ok = expires_at > now
    return ok, expires_at


async def verify_codex_session() -> tuple[bool, datetime | None]:
    """Probe Codex CLI auth session health.

    Returns:
        (ok, expires_at) where ``ok`` is True when session is valid.
        ``expires_at`` is populated from auth.json when available; None
        when determined via CLI exit code only.
    """
    settings = get_settings()
    ok, expires_at = await _probe_cli(settings.codex_bin, settings.codex_auth_probe_timeout_seconds)
    if ok:
        return True, expires_at

    # CLI probe failed or codex bin missing — try file fallback
    return _probe_auth_json(settings.codex_auth_dir)


async def _poll_loop(app: FastAPI, interval: int) -> None:
    """Internal polling coroutine — runs until cancelled."""
    prev_healthy: bool | None = None  # track transitions for targeted WARNING

    while True:
        try:
            ok, expires_at = await verify_codex_session()
        except Exception:
            ok, expires_at = False, None
            logger.warning("codex.auth.poller_unexpected_error", exc_info=True)

        app.state.codex_session_healthy = ok
        app.state.codex_session_expires_at = expires_at

        if prev_healthy is True and not ok:
            # Transition healthy → unhealthy: emit targeted structured warning.
            logger.warning(
                "codex.auth.session_went_unhealthy",
                codex_session_healthy=False,
                expires_at=expires_at.isoformat() if expires_at else None,
            )
        elif not ok:
            logger.warning(
                "codex.auth.session_unhealthy",
                codex_session_healthy=False,
                expires_at=expires_at.isoformat() if expires_at else None,
            )

        prev_healthy = ok

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


async def start_poller(app: FastAPI) -> asyncio.Task[None]:
    """Start the background session-health polling task.

    Sets ``app.state.codex_session_healthy = False`` (default-deny) before
    the first probe fires. The first probe result updates the state.

    Args:
        app: FastAPI application instance.

    Returns:
        The running ``asyncio.Task`` (caller must cancel + await on shutdown).
    """
    settings = get_settings()
    app.state.codex_session_healthy = False
    app.state.codex_session_expires_at = None

    task: asyncio.Task[None] = asyncio.create_task(
        _poll_loop(app, settings.codex_session_poll_interval_seconds),
        name="codex-auth-session-poller",
    )
    return task
