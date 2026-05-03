"""
Async subprocess wrapper that turns ``codex exec --json`` stdout into a
typed async iterator of ``CodexEvent`` objects.

C3 contract: runner is data-yielding only. SSE keepalive is the route
layer's responsibility (uses ``gateway/sse_helpers.keepalive_wrap``).
Runner yield cadence: one event per JSONL line; long agent_message gaps
are normal — the keepalive layer handles them.

C1 contract: ``--ephemeral`` is only appended when
``settings.CODEX_HAS_EPHEMERAL`` is True (set by ops after
``make verify-codex`` confirms the flag exists in the pinned codex
version; defaults False so we fail-safe to no session persistence).

Caller responsibilities:
  - ``make_workspace`` / ``cleanup_workspace`` (runner never owns the dir).
  - Wrap ``run_codex`` in ``asyncio.timeout`` or pass ``timeout`` arg.
  - Catch ``WorkspaceTraversalError`` before calling run_codex.
  - Pre-check ``api_key.mode != "local-bridge"`` before calling run_codex;
    passing "local-bridge" raises ValueError (defense-in-depth only).

Subprocess safety:
  - ``start_new_session=True`` so SIGTERM hits child + all descendants.
  - Args passed as a list; never shell=True.
  - stderr capped at 64 KiB ring buffer; no unbounded accumulation.

Sandbox mode mapping (api_keys.mode → codex --sandbox flag):
  - "sandbox"      → "read-only"          (default; codex's internal sandbox)
  - "vps"          → "danger-full-access"  (container is isolation boundary)
  - "local-bridge" → ValueError raised     (never reaches runner; route returns 501)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import AsyncIterator
from pathlib import Path

import structlog

from src.codex.events import CodexEvent, ErrorEvent, ErrorPayload, TurnCompleted, TurnFailed
from src.codex.jsonl_parser import parse_line
from src.observability.metrics import (
    CODEX_ACTIVE_SUBPROCESS,
    CODEX_SUBPROCESS_DURATION,
    CODEX_SUBPROCESS_EXIT_CODE,
)
from src.settings import get_settings

logger = structlog.get_logger(__name__)

_STDERR_CAP = 64 * 1024  # 64 KiB ring buffer cap
_STDERR_TAIL = 4 * 1024  # last 4 KiB included in synthesised ErrorEvent

# Mapping from api_keys.mode to codex --sandbox flag value.
# "local-bridge" is intentionally absent — route layer returns 501 before reaching runner.
_API_MODE_TO_CODEX_SANDBOX: dict[str, str] = {
    "sandbox": "read-only",
    "vps": "danger-full-access",
}

# Valid codex --sandbox flag values accepted by run_codex directly.
_VALID_SANDBOX_MODES: frozenset[str] = frozenset(
    {"read-only", "workspace-write", "danger-full-access"}
)


def resolve_sandbox_flag(api_key_mode: str) -> str:
    """Map api_keys.mode → codex --sandbox value.

    Args:
        api_key_mode: Value from api_keys.mode column ("sandbox", "vps", etc.).

    Returns:
        The codex --sandbox flag value to pass on the CLI.

    Raises:
        ValueError: If the mode has no mapping (e.g. "local-bridge" — caller
                    must handle this before reaching the runner).
    """
    try:
        return _API_MODE_TO_CODEX_SANDBOX[api_key_mode]
    except KeyError as exc:
        raise ValueError(f"unsupported codex api_key mode: {api_key_mode!r}") from exc


async def _drain_stderr(
    stream: asyncio.StreamReader,
    buf: bytearray,
    cap: int,
) -> None:
    """Read stderr into a capped ring buffer until EOF."""
    try:
        async for chunk in stream:
            buf.extend(chunk)
            if len(buf) > cap:
                # Drop oldest bytes — keep newest ``cap`` bytes
                del buf[: len(buf) - cap]
    except asyncio.CancelledError:
        pass


async def _terminate(proc: asyncio.subprocess.Process, grace: float, pgid: int) -> None:
    """SIGTERM → SIGKILL escalation on the process group.

    ``pgid`` must be captured at spawn (H-2: never re-derive via os.getpgid).
    Falls back to proc.terminate()/kill() when pgid is 0.
    """
    if proc.returncode is not None:
        return
    try:
        if pgid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except TimeoutError:
        try:
            if pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass


async def run_codex(
    prompt: str,
    *,
    sandbox_mode: str,
    workspace_dir: Path,
    timeout: float,
    model: str | None = None,
    search: bool = False,
    request_id: str | None = None,
) -> AsyncIterator[CodexEvent]:
    """Spawn ``codex exec --json``; yield typed events; SIGTERM/SIGKILL on cancel/timeout.

    Args:
        sandbox_mode: Codex --sandbox flag value. Must be one of
                      {"read-only", "workspace-write", "danger-full-access"}.
                      Use ``resolve_sandbox_flag(api_key.mode)`` at the call site
                      to map from an api_keys.mode value.
    """
    if sandbox_mode not in _VALID_SANDBOX_MODES:
        raise ValueError(
            f"invalid sandbox_mode {sandbox_mode!r}; "
            f"must be one of {sorted(_VALID_SANDBOX_MODES)}"
        )
    settings = get_settings()

    # C-1 fix: build argv linearly — positional insert shifts tokens and can
    # split argument pairs (e.g. insert(4,…) puts --ephemeral between
    # "--color" and "never"). Append conditional flags explicitly.
    # NOTE: codex 0.125.0 does NOT expose --ask-for-approval (researcher-01 drift).
    argv = [
        settings.codex_bin,
        "exec",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--cd",
        str(workspace_dir),
    ]

    # C1: only append --ephemeral when verify-codex.sh has confirmed the flag
    # exists in the pinned codex version. Defaults False → no session persistence
    # claim; still functional via per-request --cd workspace.
    if settings.codex_has_ephemeral:
        argv.append("--ephemeral")

    # Sandbox dispatch:
    #   danger-full-access (vps mode) → bypass codex's sandbox entirely. The
    #     gateway Docker container is the isolation boundary; codex's vendored
    #     bwrap requires unprivileged user namespaces which most Docker hosts
    #     do not allow, so --sandbox danger-full-access still spawns bwrap and
    #     fails. --dangerously-bypass-approvals-and-sandbox skips bwrap and is
    #     codex's documented "trust the surrounding container" mode.
    #   read-only / workspace-write → keep --full-auto + --sandbox so codex
    #     uses its internal sandbox policy.
    if sandbox_mode == "danger-full-access":
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["--full-auto", "--sandbox", sandbox_mode]

    if model:
        argv += ["-m", model]
    if search:
        argv += ["--search"]

    argv.append(prompt)

    # C-2 fix: dual auth env vars for robustness.
    # CODEX_HOME: codex 0.125+ reads auth from <CODEX_HOME>/auth.json directly.
    # HOME fallback: older codex resolves auth at $HOME/.codex/auth.json —
    #   set HOME=parent(auth_dir) so it works when auth_dir is /root/.codex.
    #   (With legacy /codex-auth default, parent=/ is wrong; CODEX_HOME wins.)
    # Recommended mount: ~/.codex:/root/.codex:ro + CODEX_AUTH_DIR=/root/.codex
    env = {
        **os.environ,
        "CODEX_HOME": settings.codex_auth_dir,
        "HOME": str(Path(settings.codex_auth_dir).parent),
        "CODEX_REQUEST_ID": request_id or "",
    }

    log = logger.bind(
        request_id=request_id,
        workspace=str(workspace_dir),
        sandbox_mode=sandbox_mode,
    )
    log.debug("codex.runner.spawning", argv=argv)

    _run_start = time.monotonic()
    CODEX_ACTIVE_SUBPROCESS.inc()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # SIGTERM hits whole process group
        env=env,
    )
    # H-2 fix: PGID == PID at spawn (start_new_session=True → child is group leader).
    # Capture once; never re-derive via os.getpgid after spawn (PID reuse race).
    _pgid: int = proc.pid

    stderr_buf: bytearray = bytearray()
    stderr_task = asyncio.create_task(
        _drain_stderr(proc.stderr, stderr_buf, _STDERR_CAP),  # type: ignore[arg-type]
        name=f"stderr-drain-{request_id or proc.pid}",
    )

    saw_terminal = False
    try:
        async with asyncio.timeout(timeout):
            assert proc.stdout is not None  # always set when PIPE
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace")
                evt = parse_line(line)
                if evt is None:
                    continue
                if isinstance(evt, TurnCompleted | TurnFailed | ErrorEvent):
                    saw_terminal = True
                yield evt

    except TimeoutError:
        log.warning("codex.runner.timeout", timeout=timeout)
        await _terminate(proc, float(settings.job_cancel_grace_seconds), _pgid)
        yield ErrorEvent(
            type="error",
            error=ErrorPayload(code="TIMEOUT", message=f"exceeded {timeout}s"),
        )
        return

    except (asyncio.CancelledError, GeneratorExit):
        log.info("codex.runner.cancelled")
        await _terminate(proc, float(settings.job_cancel_grace_seconds), _pgid)
        raise

    finally:
        stderr_task.cancel()
        # H-1 fix: narrow suppression to CancelledError only (don't mask
        # KeyboardInterrupt / SystemExit). Combine with Exception suppression
        # so that proc.wait() errors don't silently swallow the CancelledError.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await proc.wait()
        CODEX_ACTIVE_SUBPROCESS.dec()
        CODEX_SUBPROCESS_DURATION.labels(
            exit_code_class="0" if (proc.returncode or 0) == 0 else "nonzero"
        ).observe(time.monotonic() - _run_start)

    rc = proc.returncode
    log.debug("codex.runner.exited", exit_code=rc)
    if rc is not None:
        CODEX_SUBPROCESS_EXIT_CODE.labels(code=str(rc)).inc()

    if rc is None:
        # H-1: proc.wait() was interrupted; treat as abnormal exit.
        log.warning("codex.runner.wait_interrupted")
        return

    if rc != 0 and not saw_terminal:
        tail = stderr_buf[-_STDERR_TAIL:].decode("utf-8", errors="replace")
        log.warning("codex.runner.nonzero_exit", exit_code=rc)
        yield ErrorEvent(
            type="error",
            error=ErrorPayload(
                code="EXIT_NONZERO",
                message=f"codex exited {rc}",
                details={"stderr_tail": tail},
            ),
        )
