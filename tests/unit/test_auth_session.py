"""
Unit tests for src/codex/auth_session.py.

Covers:
- verify_codex_session: CLI exit-0 → healthy
- verify_codex_session: CLI exit-1 + auth.json present → fallback
- verify_codex_session: CLI missing + auth.json present → fallback
- verify_codex_session: CLI exit-1 + auth.json absent → (False, None)
- verify_codex_session: auth.json with future expires_at → (True, datetime)
- verify_codex_session: auth.json with past expires_at → (False, datetime)
- start_poller: sets app.state.codex_session_healthy
- Poller cancel: CancelledError handled cleanly
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.codex.auth_session import start_poller, verify_codex_session

# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_proc(returncode: int) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = AsyncMock()
    proc.stderr = AsyncMock()

    async def _wait() -> int:
        return returncode

    proc.wait = _wait
    return proc


def _settings(bin_: str = "codex", auth_dir: str = "/fake-auth", timeout: int = 3) -> MagicMock:
    s = MagicMock()
    s.codex_bin = bin_
    s.codex_auth_dir = auth_dir
    s.codex_auth_probe_timeout_seconds = timeout
    s.codex_session_poll_interval_seconds = 1
    return s


# ── verify_codex_session ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_session_cli_exit_zero_is_healthy(tmp_path: Path) -> None:
    proc = _mock_proc(0)
    settings = _settings(auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        ok, expires_at = await verify_codex_session()

    assert ok is True
    assert expires_at is None


@pytest.mark.asyncio
async def test_verify_session_cli_fails_falls_back_to_auth_json_no_expiry(tmp_path: Path) -> None:
    """H-4: CLI exit-1 + auth.json without expires_at → unhealthy (fail-closed).

    The CLI probe failed (codex itself says session broken). An auth.json with no
    expiry field must NOT override that verdict.
    """
    (tmp_path / "auth.json").write_text(json.dumps({"token": "abc"}))
    proc = _mock_proc(1)
    settings = _settings(auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        ok, expires_at = await verify_codex_session()

    assert ok is False  # H-4: no expiry → unhealthy, not assume valid
    assert expires_at is None


@pytest.mark.asyncio
async def test_verify_session_auth_json_future_expiry(tmp_path: Path) -> None:
    future = datetime.now(tz=UTC) + timedelta(hours=1)
    (tmp_path / "auth.json").write_text(json.dumps({"expires_at": future.isoformat()}))
    proc = _mock_proc(1)
    settings = _settings(auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        ok, expires_at = await verify_codex_session()

    assert ok is True
    assert expires_at is not None
    assert expires_at > datetime.now(tz=UTC)


@pytest.mark.asyncio
async def test_verify_session_auth_json_past_expiry(tmp_path: Path) -> None:
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    (tmp_path / "auth.json").write_text(json.dumps({"expires_at": past.isoformat()}))
    proc = _mock_proc(1)
    settings = _settings(auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        ok, expires_at = await verify_codex_session()

    assert ok is False
    assert expires_at is not None


@pytest.mark.asyncio
async def test_verify_session_no_auth_json_and_cli_fails(tmp_path: Path) -> None:
    proc = _mock_proc(1)
    settings = _settings(auth_dir=str(tmp_path))  # tmp_path has no auth.json

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        ok, expires_at = await verify_codex_session()

    assert ok is False
    assert expires_at is None


@pytest.mark.asyncio
async def test_verify_session_bin_not_found_falls_back(tmp_path: Path) -> None:
    """Binary missing → fallback to auth.json. No expiry → unhealthy (H-4)."""
    (tmp_path / "auth.json").write_text(json.dumps({}))
    settings = _settings(bin_="no-such-bin", auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
    ):
        ok, _ = await verify_codex_session()

    assert ok is False  # H-4: file exists but no expiry field → unhealthy


@pytest.mark.asyncio
async def test_verify_session_bin_not_found_with_future_expiry(tmp_path: Path) -> None:
    """Binary missing → fallback to auth.json. Future expiry → healthy."""
    future = datetime.now(tz=UTC) + timedelta(hours=1)
    (tmp_path / "auth.json").write_text(json.dumps({"expires_at": future.isoformat()}))
    settings = _settings(bin_="no-such-bin", auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
    ):
        ok, expires_at = await verify_codex_session()

    assert ok is True  # binary missing but valid auth.json with future expiry
    assert expires_at is not None


@pytest.mark.asyncio
async def test_probe_cli_tries_both_variants_regardless_of_exit_code(tmp_path: Path) -> None:
    """H-5: both subcommand variants must be tried even on non-128 non-zero exits.

    If 'codex auth status' returns exit 2 (argparse unknown subcommand) the old
    code returned (False, None) immediately, never trying 'codex login status'.
    """
    calls: list[tuple[str, ...]] = []
    procs = [_mock_proc(2), _mock_proc(0)]  # first variant exit-2, second exit-0
    proc_iter = iter(procs)

    async def _fake_exec(codex_bin: str, *subargs: str, **kwargs: object) -> MagicMock:
        calls.append((codex_bin, *subargs))
        return next(proc_iter)

    settings = _settings(auth_dir=str(tmp_path))

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        ok, _ = await verify_codex_session()

    # Both variants must have been called
    assert len(calls) == 2, f"Expected 2 probe attempts, got {len(calls)}: {calls}"
    assert calls[0][1:] == ("auth", "status"), f"First call must be auth status: {calls[0]}"
    assert calls[1][1:] == ("login", "status"), f"Second call must be login status: {calls[1]}"
    assert ok is True  # second variant returned 0


# ── start_poller ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_poller_sets_healthy_state(tmp_path: Path) -> None:
    """start_poller sets default-deny (False) synchronously, then poller probes."""
    proc = _mock_proc(0)
    settings = _settings(auth_dir=str(tmp_path))
    settings.codex_session_poll_interval_seconds = 9999  # prevent second loop iteration

    app = MagicMock()
    app.state = MagicMock()

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        # Default-deny must be set synchronously inside start_poller before
        # returning, so we can assert it immediately without any await.
        task = await start_poller(app)
        # After creation, before any event loop cycles, state is False.
        assert app.state.codex_session_healthy is False  # default-deny

        # Give event loop cycles to let poller run the first probe (CLI exit 0 → True).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # After the probe, healthy transitions to True.
        assert app.state.codex_session_healthy is True

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_poller_cancelled_error_handled_cleanly(tmp_path: Path) -> None:
    """Cancelling the poll task must not raise in the caller."""
    proc = _mock_proc(0)
    settings = _settings(auth_dir=str(tmp_path))
    settings.codex_session_poll_interval_seconds = 9999

    app = MagicMock()
    app.state = MagicMock()

    with (
        patch("src.codex.auth_session.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        task = await start_poller(app)
        await asyncio.sleep(0)
        task.cancel()
        # Must NOT raise after cancellation
        with contextlib.suppress(asyncio.CancelledError):
            await task
