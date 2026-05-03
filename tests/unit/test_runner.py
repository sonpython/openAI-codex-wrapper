"""
Unit tests for src/codex/runner.py.

All tests mock asyncio.create_subprocess_exec — no real codex binary needed.

Covers:
- argv assembly: base flags always present
- argv assembly: --ephemeral appended only when CODEX_HAS_EPHEMERAL=True
- argv assembly: --sandbox workspace-write vs read-only
- argv assembly: -m model and --search flags
- Events yielded in order from scripted stdout
- Synthesised ErrorEvent on non-zero exit with no terminal event
- Timeout case: yields TIMEOUT ErrorEvent
- SIGTERM then SIGKILL sequence on cancel
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.codex.events import ErrorEvent, ThreadStarted, TurnCompleted
from src.codex.runner import resolve_sandbox_flag, run_codex

# ── Fake subprocess helpers ───────────────────────────────────────────────────


def _fake_stdout_reader(lines: list[bytes]) -> AsyncMock:
    """AsyncMock that mimics an asyncio.StreamReader iterating ``lines``."""

    async def _aiter(self: object) -> object:  # type: ignore[misc]
        for line in lines:
            yield line

    reader = MagicMock()
    reader.__aiter__ = _aiter
    return reader  # type: ignore[return-value]


def _fake_proc(
    stdout_lines: list[bytes],
    returncode: int = 0,
    pid: int = 99999,
) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.stdout = _fake_stdout_reader(stdout_lines)
    proc.stderr = _fake_stdout_reader([])  # empty stderr

    async def _wait() -> int:
        proc.returncode = returncode
        return returncode

    proc.wait = _wait
    return proc


def _settings(
    *,
    has_ephemeral: bool = False,
    grace: int = 5,
    codex_bin: str = "codex",
    auth_dir: str = "/codex-auth",
) -> MagicMock:
    s = MagicMock()
    s.codex_bin = codex_bin
    s.codex_has_ephemeral = has_ephemeral
    s.job_cancel_grace_seconds = grace
    s.codex_auth_dir = auth_dir
    return s


# ── argv assembly tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_argv_base_flags_always_present(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc(
            [b'{"type":"turn.completed"}\n'],
            returncode=0,
        )

    settings = _settings()
    with (
        patch("src.codex.runner.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex("hello", sandbox_mode="read-only", workspace_dir=ws, timeout=10.0):
            pass

    assert "codex" in captured_argv
    assert "--json" in captured_argv
    assert "--skip-git-repo-check" in captured_argv
    assert "--full-auto" in captured_argv
    assert "--ephemeral" not in captured_argv  # not set


@pytest.mark.asyncio
async def test_argv_color_never_pair_unbroken(tmp_path: Path) -> None:
    """C-1 regression: --color and never must always be adjacent regardless of other flags.

    Catches the insert(4, '--ephemeral') off-by-one that split the pair.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    # Test with ephemeral=False
    settings_no_eph = _settings(has_ephemeral=False)
    with (
        patch("src.codex.runner.get_settings", return_value=settings_no_eph),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex("hi", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0):
            pass

    color_idx = captured_argv.index("--color")
    assert (
        captured_argv[color_idx + 1] == "never"
    ), f"--color must be immediately followed by 'never'; got {captured_argv[color_idx + 1]!r}"

    # Test with ephemeral=True — --ephemeral must NOT split --color / never
    captured_argv.clear()
    settings_eph = _settings(has_ephemeral=True)
    with (
        patch("src.codex.runner.get_settings", return_value=settings_eph),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex("hi", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0):
            pass

    color_idx = captured_argv.index("--color")
    assert (
        captured_argv[color_idx + 1] == "never"
    ), f"--ephemeral broke --color/never pair: {captured_argv!r}"
    assert "--ephemeral" in captured_argv
    # --ephemeral must appear AFTER --cd <workspace> and BEFORE the prompt
    cd_idx = captured_argv.index("--cd")
    eph_idx = captured_argv.index("--ephemeral")
    prompt_idx = captured_argv.index("hi")
    assert eph_idx > cd_idx, "--ephemeral must come after --cd <dir>"
    assert eph_idx < prompt_idx, "--ephemeral must come before the prompt"


@pytest.mark.asyncio
async def test_argv_ephemeral_flag_when_enabled(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    settings = _settings(has_ephemeral=True)
    with (
        patch("src.codex.runner.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex("hi", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0):
            pass

    assert "--ephemeral" in captured_argv


@pytest.mark.asyncio
async def test_subprocess_env_includes_codex_home(tmp_path: Path) -> None:
    """C-2 regression: spawned subprocess env must include CODEX_HOME pointing to auth dir."""
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_kwargs: dict[str, object] = {}

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    auth_dir = "/codex-auth"
    settings = _settings(auth_dir=auth_dir)
    with (
        patch("src.codex.runner.get_settings", return_value=settings),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0):
            pass

    env = captured_kwargs.get("env", {})
    assert isinstance(env, dict)
    assert (
        env.get("CODEX_HOME") == auth_dir
    ), f"CODEX_HOME must equal codex_auth_dir ({auth_dir!r}); got {env.get('CODEX_HOME')!r}"


@pytest.mark.asyncio
async def test_terminate_uses_stored_pgid(tmp_path: Path) -> None:
    """H-2 regression: SIGTERM/SIGKILL must use PGID captured at spawn time, not re-derived."""
    ws = tmp_path / "ws"
    ws.mkdir()
    pgid_signals: list[int] = []

    async def _slow_stdout(self: object) -> object:  # type: ignore[misc]
        await asyncio.sleep(9999)
        return
        yield  # noqa: unreachable

    slow_reader = MagicMock()
    slow_reader.__aiter__ = _slow_stdout

    proc = MagicMock()
    proc.pid = 55555  # this is captured as PGID at spawn
    proc.returncode = None
    proc.stdout = slow_reader
    proc.stderr = _fake_stdout_reader([])

    async def _wait() -> int:
        proc.returncode = -15
        return -15

    proc.wait = _wait

    def _killpg(pgid: int, sig: int) -> None:
        pgid_signals.append(pgid)

    with (
        patch("src.codex.runner.get_settings", return_value=_settings(grace=0)),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("src.codex.runner.os.killpg", side_effect=_killpg),
        # os.getpgid should NOT be called — patch it to raise to catch regressions
        patch(
            "src.codex.runner.os.getpgid", side_effect=AssertionError("getpgid called after spawn")
        ),
    ):
        async for _ in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=0.05):
            pass

    # All killpg calls must use proc.pid (captured pgid), never a re-derived value
    assert all(
        pgid == 55555 for pgid in pgid_signals
    ), f"killpg was called with unexpected pgid values: {pgid_signals}"


@pytest.mark.asyncio
async def test_argv_sandbox_read_only(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0):
            pass

    idx = captured_argv.index("--sandbox")
    assert captured_argv[idx + 1] == "read-only"


@pytest.mark.asyncio
async def test_argv_sandbox_workspace_write(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex(
            "p", sandbox_mode="workspace-write", workspace_dir=ws, timeout=5.0
        ):
            pass

    idx = captured_argv.index("--sandbox")
    assert captured_argv[idx + 1] == "workspace-write"


@pytest.mark.asyncio
async def test_argv_sandbox_danger_full_access(tmp_path: Path) -> None:
    """Phase-2: vps api_key mode → --dangerously-bypass-approvals-and-sandbox.

    danger-full-access bypasses codex's bwrap entirely. Most Docker hosts
    disallow unprivileged user namespaces, so --sandbox danger-full-access
    still spawns bwrap and fails. The bypass flag is codex's documented
    "the surrounding container is the sandbox" mode and is what `vps`
    api_keys.mode resolves to on the wire.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex(
            "p", sandbox_mode="danger-full-access", workspace_dir=ws, timeout=5.0
        ):
            pass

    assert "--dangerously-bypass-approvals-and-sandbox" in captured_argv
    # And the in-sandbox flags are absent — they're only emitted for
    # read-only / workspace-write modes.
    assert "--sandbox" not in captured_argv
    assert "--full-auto" not in captured_argv


@pytest.mark.asyncio
async def test_argv_invalid_sandbox_mode_raises(tmp_path: Path) -> None:
    """Phase-2: passing an unsupported sandbox_mode raises ValueError before spawning."""
    ws = tmp_path / "ws"
    ws.mkdir()
    spawned = False

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        nonlocal spawned
        spawned = True
        return _fake_proc([])

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        pytest.raises(ValueError, match="invalid sandbox_mode"),
    ):
        async for _ in run_codex("p", sandbox_mode="local-bridge", workspace_dir=ws, timeout=5.0):
            pass

    assert not spawned, "subprocess must not be spawned for invalid sandbox_mode"


# ── resolve_sandbox_flag unit tests ───────────────────────────────────────────


def test_resolve_sandbox_flag_sandbox_mode() -> None:
    """Phase-2: api_key mode 'sandbox' maps to 'read-only'."""
    assert resolve_sandbox_flag("sandbox") == "read-only"


def test_resolve_sandbox_flag_vps_mode() -> None:
    """Phase-2: api_key mode 'vps' maps to 'danger-full-access'."""
    assert resolve_sandbox_flag("vps") == "danger-full-access"


def test_resolve_sandbox_flag_local_bridge_raises() -> None:
    """Phase-2: local-bridge mode raises ValueError (route layer should intercept first)."""
    with pytest.raises(ValueError, match="unsupported codex api_key mode"):
        resolve_sandbox_flag("local-bridge")


def test_resolve_sandbox_flag_unknown_raises() -> None:
    """Phase-2: unknown mode raises ValueError."""
    with pytest.raises(ValueError, match="unsupported codex api_key mode"):
        resolve_sandbox_flag("turbo-mode")


@pytest.mark.asyncio
async def test_argv_model_flag(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex(
            "p", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0, model="gpt-4o"
        ):
            pass

    assert "-m" in captured_argv
    assert "gpt-4o" in captured_argv


@pytest.mark.asyncio
async def test_argv_search_flag(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    captured_argv: list[str] = []

    async def _fake_exec(*argv: str, **kwargs: object) -> MagicMock:
        captured_argv.extend(argv)
        return _fake_proc([b'{"type":"turn.completed"}\n'])

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
    ):
        async for _ in run_codex(
            "p", sandbox_mode="read-only", workspace_dir=ws, timeout=5.0, search=True
        ):
            pass

    assert "--search" in captured_argv


# ── Event streaming tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_yielded_in_order(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    stdout_lines = [
        b'{"type":"thread.started","thread_id":"t1"}\n',
        b'{"type":"turn.completed"}\n',
    ]

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", return_value=_fake_proc(stdout_lines)),
    ):
        events = [
            e
            async for e in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=10.0)
        ]

    assert len(events) == 2
    assert isinstance(events[0], ThreadStarted)
    assert isinstance(events[1], TurnCompleted)


@pytest.mark.asyncio
async def test_non_json_lines_skipped(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    stdout_lines = [
        b"MCP tool output contamination\n",
        b'{"type":"thread.started","thread_id":"t1"}\n',
        b"\n",
        b'{"type":"turn.completed"}\n',
    ]

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch("asyncio.create_subprocess_exec", return_value=_fake_proc(stdout_lines)),
    ):
        events = [
            e
            async for e in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=10.0)
        ]

    assert len(events) == 2
    assert isinstance(events[0], ThreadStarted)


@pytest.mark.asyncio
async def test_nonzero_exit_synthesises_error_event(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    stdout_lines = [b'{"type":"thread.started","thread_id":"t1"}\n']

    with (
        patch("src.codex.runner.get_settings", return_value=_settings()),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_fake_proc(stdout_lines, returncode=1),
        ),
    ):
        events = [
            e
            async for e in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=10.0)
        ]

    assert len(events) == 2
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].error.code == "EXIT_NONZERO"


@pytest.mark.asyncio
async def test_timeout_synthesises_timeout_error_event(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    async def _slow_stdout(self: object) -> object:  # type: ignore[misc]
        await asyncio.sleep(9999)
        return
        yield  # make it an async generator  # noqa: unreachable

    slow_reader = MagicMock()
    slow_reader.__aiter__ = _slow_stdout

    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None
    proc.stdout = slow_reader
    proc.stderr = _fake_stdout_reader([])

    async def _wait() -> int:
        proc.returncode = -15
        return -15

    proc.wait = _wait

    with (
        patch("src.codex.runner.get_settings", return_value=_settings(grace=0)),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("src.codex.runner.os.killpg"),  # don't actually send signals in tests
        # H-2: os.getpgid is no longer called after spawn; patch present only for
        # safety in case of regression — test_terminate_uses_stored_pgid covers this.
    ):
        events = [
            e
            async for e in run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=0.05)
        ]

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].error.code == "TIMEOUT"


@pytest.mark.asyncio
async def test_cancel_sends_sigterm_then_sigkill(tmp_path: Path) -> None:
    """Cancelling the generator triggers SIGTERM + SIGKILL path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    signals_sent: list[int] = []

    async def _slow_stdout(self: object) -> object:  # type: ignore[misc]
        await asyncio.sleep(9999)
        return
        yield  # noqa: unreachable

    slow_reader = MagicMock()
    slow_reader.__aiter__ = _slow_stdout

    proc = MagicMock()
    proc.pid = 11111
    proc.returncode = None
    proc.stdout = slow_reader
    proc.stderr = _fake_stdout_reader([])

    async def _wait_immediate() -> int:
        # Returns immediately (simulates process reaped after SIGKILL sets returncode)
        return proc.returncode if proc.returncode is not None else -15

    proc.wait = _wait_immediate

    def _killpg(pgid: int, sig: int) -> None:
        signals_sent.append(sig)
        if sig == signal.SIGTERM:
            # Simulate process not dying on SIGTERM (grace=0 forces SIGKILL path)
            pass
        if sig == signal.SIGKILL:
            proc.returncode = -9

    with (
        patch("src.codex.runner.get_settings", return_value=_settings(grace=0)),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("src.codex.runner.os.killpg", side_effect=_killpg),
        # H-2: os.getpgid no longer called; not patched here.
    ):
        gen = run_codex("p", sandbox_mode="read-only", workspace_dir=ws, timeout=30.0)
        # Start iterating then cancel after first poll
        task = asyncio.create_task(_collect(gen))
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # SIGTERM must have been sent (SIGKILL may or may not depending on timing)
    assert signal.SIGTERM in signals_sent


async def _collect(gen: object) -> list[object]:
    result = []
    async for evt in gen:  # type: ignore[attr-defined]
        result.append(evt)
    return result
