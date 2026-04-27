# Code Review — Phase 02 Codex Runner

**Reviewer:** code-reviewer
**Date:** 2026-04-27 17:57
**Scope:** `src/codex/*.py`, `src/gateway/{app,health}.py` lifespan/readyz wiring, `src/settings.py` new fields, `tests/unit/test_{jsonl_parser,workspace,auth_session,runner}.py`, `tests/integration/test_runner_smoke.py`, `tests/fixtures/jsonl/*.jsonl`.
**Spec:** `phase-02-codex-runner.md`.

---

## Overall Assessment

The phase-02 implementation lands the four-module layout cleanly: events / parser / workspace / auth-session / runner each stay under the 200-LOC budget, naming is consistent, the C6 (realpath+commonpath) and C1 (`CODEX_HAS_EPHEMERAL` gate) fixes are honoured at the design level, and the parser is satisfyingly tolerant. Tests cover the happy paths and most edge cases.

**However**, two production-breaking bugs slipped past tests because they only manifest under specific runtime configurations (`codex_has_ephemeral=True`, default `codex_auth_dir=/codex-auth`). Both are CI-green but break under live codex. Plus several high-severity issues around process reaping, exit-code reads, and a `getattr` default that fails-open on `/readyz`.

**Verdict:** APPROVE_WITH_CHANGES. Two CRITICAL must-fix before phase-03 consumption; four HIGH worth addressing in same PR.

---

## Critical Issues (BLOCKING)

### C-1 — `--ephemeral` insertion splits `--color never` argument pair
**File:** `src/codex/runner.py:110-111`
**Trigger:** `settings.codex_has_ephemeral=True` (the explicit C1 happy-path).
**Severity:** CRITICAL — codex CLI invocation fails with arg-parse error every time the feature flag is on.

The base `argv` list is built as:

```
[0] codex_bin
[1] "exec"
[2] "--json"
[3] "--color"
[4] "never"           ← VALUE for --color
[5] "--skip-git-repo-check"
[6] "--cd"
[7] workspace_dir
...
```

`argv.insert(4, "--ephemeral")` puts `--ephemeral` BEFORE index 4 (`"never"`), producing:

```
codex exec --json --color --ephemeral never --skip-git-repo-check ...
```

`--color` now takes `--ephemeral` as its value (or codex errors out about an unknown color). The intent — per the spec comment "before `--skip-git-repo-check`" — is index 5, not 4. The off-by-one happens because the spec author treated `--color never` as one logical token but the list stores them as two.

**The runner unit test `test_argv_ephemeral_flag_when_enabled` only asserts `"--ephemeral" in captured_argv` — it never checks position relative to `--color`, so this bug ships green.**

**Fix:** change `argv.insert(4, "--ephemeral")` to `argv.insert(5, "--ephemeral")`, OR (safer) append after the base list before the optional flags:

```python
if settings.codex_has_ephemeral:
    argv.append("--ephemeral")
```

(`codex exec` accepts flags in any position; pre-prompt is fine. Make sure the flag does not land *after* the prompt at the very end, since `argv.append(prompt)` runs unconditionally below.)

**Test gap to plug:** assert `argv.index("--color") + 1` resolves to `"never"` regardless of `codex_has_ephemeral`.

---

### C-2 — Default `codex_auth_dir=/codex-auth` makes `HOME` derivation point to filesystem root
**Files:** `src/settings.py:50`, `src/codex/runner.py:120-124`.
**Trigger:** any spawn under default settings (no env override).
**Severity:** CRITICAL — codex cannot find `~/.codex/auth.json`, every request fails auth.

Runner sets:

```python
"HOME": str(Path(settings.codex_auth_dir).parent)
```

With `codex_auth_dir = "/codex-auth"` (the documented default), `Path("/codex-auth").parent == Path("/")`. Codex resolves `~/.codex` → `/.codex` → does not exist → auth probe fails on every spawn. The intent (per phase-00 spec) is that `codex_auth_dir` is the directory **containing** `auth.json`, and codex expects that directory to be named `.codex` and live one level under `$HOME`.

Two concrete mismatches:
1. `codex_auth_dir = "/codex-auth"` is NOT named `.codex`. Codex looks for `$HOME/.codex/auth.json`. Setting `HOME=/` makes codex look at `/.codex/auth.json`, not `/codex-auth/auth.json`.
2. `auth_session._probe_auth_json` reads `<auth_dir>/auth.json` directly (correct) — so the *probe* works, but the *runner* does not, producing inconsistent state: `/readyz` reports healthy while live requests fail with auth errors.

**Recommended fixes** (in priority order):
1. Either rename the default to `/root/.codex` (or container-friendly `/home/codex/.codex`) AND change runner to `HOME = Path(settings.codex_auth_dir).parent` (now correct), OR
2. Keep current default but symlink/bind-mount the dir to be named `.codex`, OR
3. Stop deriving HOME — instead set `CODEX_HOME` env (if codex supports it) or pass an explicit `--config-dir` flag (research-01 §5 didn't mention one — would require verification).

This bug is invisible in unit tests because `test_runner.py` patches `get_settings` with `auth_dir="/codex-auth"` but never inspects the resulting `env` dict. **Add an assertion that the spawned env has `HOME` resolving to a real, accessible parent of `<auth_dir>/auth.json`.**

Cross-link: `auth_session.py:120` reads `<codex_auth_dir>/auth.json` at the documented path and "works"; `runner.py:123` reads the parent and silently breaks. The two modules must agree on what `codex_auth_dir` means.

---

## High Priority

### H-1 — `proc.returncode` read after `finally` block can be `None` if `proc.wait()` was suppressed
**File:** `src/codex/runner.py:175-194`.

```python
finally:
    stderr_task.cancel()
    with contextlib.suppress(BaseException):
        await proc.wait()

rc = proc.returncode
log.debug("codex.runner.exited", exit_code=rc)

if rc != 0 and not saw_terminal:
    ...
```

The `with contextlib.suppress(BaseException)` swallows any error from `proc.wait()` (including `CancelledError` and even `KeyboardInterrupt`). If wait fails, `proc.returncode` may still be `None`, and the `rc != 0` comparison reads `None != 0` → True → emits an `EXIT_NONZERO` event with `f"codex exited None"`. Not catastrophic, but misleading and tests cannot distinguish "real non-zero exit" from "wait was interrupted."

Also: `BaseException` is overly aggressive. `CancelledError` is the realistic risk here; `BaseException` masks `KeyboardInterrupt`/`SystemExit` which the caller might intentionally want to propagate during shutdown.

**Fix:**
```python
finally:
    stderr_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        with contextlib.suppress(Exception):
            await proc.wait()

rc = proc.returncode
if rc is None:
    # wait was interrupted; treat as abnormal but distinct
    log.warning("codex.runner.wait_interrupted")
elif rc != 0 and not saw_terminal:
    ...
```

---

### H-2 — `_terminate` uses `os.getpgid(proc.pid)` which races with reap
**File:** `src/codex/runner.py:63-75`.

`os.killpg(os.getpgid(proc.pid), ...)` looks up the PGID *just before* signalling. Between the `proc.returncode is not None` check (line 65) and the `os.getpgid` call (line 68/75), the kernel could reap the process and reuse the PID for an unrelated process group. Then we'd SIGTERM/SIGKILL an innocent process group.

This is a low-likelihood but high-impact bug (could kill the parent gateway under PID wraparound on a busy host). Two mitigations:
1. Capture PGID once at spawn time (since `start_new_session=True`, PGID == PID at spawn): store `pgid = proc.pid` immediately after `create_subprocess_exec`. Use that captured value, NOT `os.getpgid(proc.pid)` after the fact.
2. Re-check `proc.returncode is not None` between SIGTERM and SIGKILL — currently the SIGKILL branch only catches `ProcessLookupError`, which won't fire if the PID got reused.

**Reference:** also affects `_terminate`'s second `os.killpg` call (line 75) — same race.

---

### H-3 — `/readyz` defaults to `True` when `codex_session_healthy` attribute is missing
**File:** `src/gateway/health.py:78`.

```python
session_healthy: bool = getattr(request.app.state, "codex_session_healthy", True)
```

Default `True` violates the explicit spec requirement (phase-02 §Implementation Steps 7: "default-deny on startup (False until first probe succeeds) — fail-closed"). The justification in the comment ("guard with getattr for bare test apps that don't run the full lifespan") is reasonable for tests, but production should fail-closed.

Two safer paths:
1. Default `False` here AND have test fixtures set `app.state.codex_session_healthy = True` explicitly. Closes the gap.
2. Default `False` here AND have the lifespan set the attribute in the FIRST line of startup (before any other init can fail), so any time the lifespan completed at all, the attribute exists.

Currently `start_poller` sets the attribute at line 172, which IS early enough — meaning the default-True branch is only reachable in tests OR if the lifespan hasn't run yet. Either way, prefer `False`.

---

### H-4 — Auth session poller silently treats missing `expires_at` as healthy
**File:** `src/codex/auth_session.py:90-93`.

```python
raw_exp = data.get("expires_at")
if not raw_exp:
    # No expiry field — assume valid if file exists
    return True, None
```

Per spec §Key Insights: "We won't trust the timestamp blindly; `codex auth status` is authoritative. Fallback if CLI subcommand absent: parse `auth.json` mtime + `expires_at`."

The fallback is reached when the CLI probe ALREADY failed (`_probe_cli` returned `(False, None)`). That means codex itself believes the session is broken. Returning `(True, None)` purely because the file exists overrides codex's own judgement. Better fallback is to either:
- Trust ONLY when `expires_at` is present AND in the future. No expiry → unhealthy.
- Use file mtime as a TTL signal (e.g. unhealthy if older than 7 days).

Current behaviour: a stale auth.json with no `expires_at` field reports healthy forever. `/readyz` lies; first real request fails with codex error.

Test gap: `test_verify_session_cli_fails_falls_back_to_auth_json` (line 71-84) asserts `ok is True` for the no-expiry case — meaning the test cements the broken behaviour. Fix the test alongside the code.

---

### H-5 — Auth probe `_probe_cli` exit-code logic does not advance to fallback subcommand
**File:** `src/codex/auth_session.py:60-67`.

```python
if proc.returncode == 0:
    return True, None
# Exit 128 usually means "subcommand not found"; try next variant.
if proc.returncode != 128:
    return False, None
```

Comment says exit 128 = "subcommand not found". In practice, "command not found" from a shell is exit 127; "killed by signal N" reports as 128+N (e.g. SIGINT → 130). Codex itself returning exit 1 for "auth not configured" is also common.

Real semantics: most CLIs report `exit 2` for "argparse error / unknown subcommand". So if `codex auth status` is renamed to `codex login status`, the first call returns exit 2, and this branch returns `(False, None)` immediately — never tries `login status`.

**Effect:** the "try both subcommand variants" forward-compat insurance documented in the spec is dead code under realistic exit codes.

**Fix options:**
1. Always try both variants regardless of exit code, return first 0.
2. Inspect stderr for "unknown subcommand" string before advancing.
3. Probe `codex --help` once to discover available subcommands, cache result.

Combine with H-4: the broader auth-session story has too many "assume healthy" fallbacks. Tighten with: explicit healthy signal required, otherwise unhealthy.

---

## Medium Priority

### M-1 — `_drain_stderr` ring-buffer trim is O(n) per chunk
**File:** `src/codex/runner.py:54-58`.

```python
async for chunk in stream:
    buf.extend(chunk)
    if len(buf) > cap:
        del buf[: len(buf) - cap]
```

`del buf[:k]` on a `bytearray` shifts the remainder — O(n) where n is `cap` (64 KiB). For a long-running job emitting MB of stderr (verbose tool output), this is a recurring 64 KiB memcpy per chunk. Acceptable for normal load; could become noticeable for jobs near the 15-min timeout with verbose codex output.

**Fix (when warranted):** use `collections.deque(maxlen=cap)` with byte-by-byte append, OR keep two halves and rotate. Not blocking for v1.

---

### M-2 — `proc.stdout` decode path doesn't strip the trailing newline before parser
**File:** `src/codex/runner.py:152-154`.

```python
async for raw in proc.stdout:
    line = raw.decode("utf-8", errors="replace")
    evt = parse_line(line)
```

`asyncio.StreamReader.__aiter__` yields lines INCLUDING the trailing `\n`. `parse_line` does `line.strip()` so this works — but if stdout produces `\r\n` (Windows / odd terminals), or if a line is the last one without a trailing `\n`, behaviour may differ. Currently fine since codex runs Linux/macOS only. Worth a passing test: feed `b'{"type":"turn.completed"}'` (no newline) and assert it still parses.

---

### M-3 — `assert proc.stdout is not None` on hot path
**File:** `src/codex/runner.py:151`.

`assert` is stripped under `-O`. If anyone runs Python with optimisation, an unexpected `None` propagates as `TypeError` instead of a clean error. Replace with explicit check:

```python
if proc.stdout is None:
    raise CodexRunnerError("subprocess did not return a stdout pipe")
```

Microscopic real-world risk, but the team's policy in `development-rules.md` is "no syntax errors / code is compilable" + handle errors. Asserts on subprocess invariants are fragile.

---

### M-4 — `verify_codex_session()` doesn't pass `start_new_session` or `env=`
**File:** `src/codex/auth_session.py:48-53`.

The auth probe spawns `codex auth status` without `start_new_session=True` and without overriding `env`. If the codex binary inherits broken environment from the gateway process (PATH issues, library paths inside Docker), or hangs and needs killing, the probe lacks the same hardening as the runner. Two consequences:
1. If the probe spawns subprocesses of its own, `proc.kill()` (line 58) hits only the immediate child, not descendants.
2. `HOME` env not set for the probe means it may use the test runner's `$HOME` (e.g. `/root` or the gateway service-account home), not the bind-mount.

Effect: probe could report False positive (uses real user's `~/.codex`) while runner uses synthetic HOME.

**Fix:** pass `env` mirror of what the runner uses (`HOME = parent(codex_auth_dir)`) and `start_new_session=True` for the probe too.

---

### M-5 — Lifespan does not fail-fast or warn loudly when first probe fails
**File:** `src/gateway/app.py:82-83`, `src/codex/auth_session.py:127-151`.

The poller starts and immediately schedules the first probe inside the loop. The lifespan returns ready (`yield`) before that first probe completes. So:

- For up to one `asyncio.sleep(interval)` cycle (0–300s), `/readyz` returns 503 even if codex is fine.
- Conversely, if codex is broken at startup, the gateway accepts traffic until `/readyz` flips. Health-check loops (Caddy / k8s) catch this within a few probes, but the spec said "spec says limp along + mark unhealthy" — current code is consistent with that, just want to flag it explicitly so it's a documented non-issue.

Consider running ONE synchronous probe inside the lifespan (with a short timeout) before yielding, so the initial state is accurate. KISS argument: don't bother, current is fine — but it should be noted in deploy runbook.

---

### M-6 — `cleanup_workspace` does not honour the spec's "logs WARNING on failure" semantics for the outside-root case
**File:** `src/codex/workspace.py:67-69`.

When `resolved` is outside `root`, the function logs and returns silently. The spec calls this "belt-and-braces" — fine. But the log call uses `logger.warning("workspace.cleanup.outside_root", ...)` which leaks the rejected path into structured logs. If a malicious caller passes `path=Path("/etc/passwd")`, this writes `/etc/passwd` into application logs. Probably fine (operator sees it; scrubber doesn't apply because it's not a user input field), but worth noting.

---

### M-7 — `proc.stdout is None` not handled when `create_subprocess_exec` fails partially
**File:** `src/codex/runner.py:134-146`.

`create_subprocess_exec` might raise `OSError` (e.g. ENOENT if binary missing, EPERM if seccomp). The runner does NOT wrap the call in try/except. The exception propagates out of `run_codex` BEFORE the workspace cleanup contract — which is OK since spec says caller owns cleanup, but the caller will see a raw `FileNotFoundError` instead of a typed `CodexRunnerError`.

Phase-03 will need to map this to a 5xx with a meaningful body. Either wrap the spawn in try/except and re-raise as `CodexRunnerError`, or document loudly in runner docstring that callers must catch `OSError`.

---

## Low Priority

### L-1 — `proc.kill()` in `_probe_cli` doesn't escalate if process ignores SIGKILL (kernel-level zombie)
**File:** `src/codex/auth_session.py:57-59`. `proc.kill()` followed by `await proc.wait()` could hang forever if the process is stuck in uninterruptible sleep (D-state). Wrap the second `proc.wait()` in another `wait_for(timeout=2)` to bound the worst case.

### L-2 — `AgentMessageItem.type: Literal["agent_message", "assistant_message"]` is a discriminator on TWO values
**File:** `src/codex/events.py:39`. Pydantic v2 discriminated unions handle this fine (each Literal value resolves to the same model class), but it's worth a regression test: feed `assistant_message` AND `agent_message` in the SAME stream — the discriminator should pick `AgentMessageItem` for both. Current `test_item_started_assistant_message_alias` covers one variant; add a dual case.

### L-3 — Logger scope: `_drain_stderr` doesn't bind `request_id`
**File:** `src/codex/runner.py:47-60`. Background stderr task's logs (none currently — but if added) won't carry request context. Pass `log` (the bound logger) into `_drain_stderr` for future-proofing.

### L-4 — `request_id` parameter is plumbed to env (`CODEX_REQUEST_ID`) but codex 0.125.0 doesn't read it
**File:** `src/codex/runner.py:124`. Harmless — the env var is just set to "" when None. Future-proofs nothing. YAGNI; remove or document it.

### L-5 — `WORKSPACE_ROOT` realpath each call in `cleanup_workspace`
**File:** `src/codex/workspace.py:62`. `Path(...).resolve()` on every cleanup call is one stat per ancestor. Cheap, but called once per request — could cache if profiling ever calls for it. Not now.

### L-6 — Tests using `MagicMock` for `proc.stdout`'s `__aiter__` are fragile
**Files:** `tests/unit/test_runner.py:32-41, 293-299, 335-341`. The pattern `reader.__aiter__ = _aiter` works but isn't idiomatic — under future Python or pytest changes, real `StreamReader` mocking via `unittest.mock.AsyncMock` with `__aiter__.return_value = ...` may be cleaner. Not blocking; tests pass today.

### L-7 — Empty fixtures for `command_execution`, `file_change` etc are not standalone files
The fixtures dir has only 9 files; `all_item_types_stream.jsonl` covers 15 events but per-type fixtures (one file per event) would make targeted regression tests easier when a single event type gets a schema bump. Spec mentioned per-type fixtures; current consolidation is fine but document the trade-off.

---

## Edge Cases Found by Scout

1. **PID wraparound during SIGTERM grace** (H-2 above) — captured.
2. **`--ephemeral` insertion off-by-one** (C-1 above) — captured.
3. **`HOME` derivation under default `codex_auth_dir`** (C-2 above) — captured.
4. **`asyncio.timeout` interaction with `GeneratorExit`** — current code catches `GeneratorExit` in the same except block as `CancelledError`, but inside the `asyncio.timeout(timeout)` context. If the generator is closed externally (caller does `await gen.aclose()`), `GeneratorExit` is raised at the most recent yield. The handler calls `_terminate` and re-raises. Good. No issue.
5. **`saw_terminal` flag and `ItemCompleted` with `agent_message`**: only `TurnCompleted/TurnFailed/ErrorEvent` set `saw_terminal=True`. Per spec §Architecture, this is correct — a stream that emits `ItemCompleted` then non-zero exit without `turn.completed` SHOULD synthesise an EXIT_NONZERO error. Verified: behaviour matches spec.
6. **Stderr drain task lifetime**: created at line 143; cancelled in `finally` at line 176. If the generator is suspended on `yield evt` and the caller never resumes, the stderr task and the subprocess both leak until garbage collection. CPython's async generator finalizer protocol calls `aclose` on collection, which triggers our `finally` — but only if the event loop is still running. **Recommended:** the route layer (phase 03) MUST use `try/finally` around `async for evt in run_codex(...)` and explicitly call `await gen.aclose()` if iteration aborts mid-stream. Document this in runner docstring.
7. **Long prompts**: `argv.append(prompt)` makes the prompt a CLI arg. `ARG_MAX` on Linux is typically 2 MiB but per-arg limits and shell quirks make >128 KiB risky. Spec mentions stdin fallback for prompts >8 KiB but it's NOT implemented. For phase-03 chat completions with conversational history, prompt > 8 KiB is realistic. **Add to phase-02 follow-up TODO.**
8. **Re-entrancy / parallel runs**: `run_codex` is an async generator with no shared state — safe for concurrent calls. Workspace dir is per-job (caller-managed) — safe. Confirmed.

---

## Positive Observations

- C6 fix in `validate_path_inside` is implemented EXACTLY as specified: realpath + commonpath, no `relative_to`, no ValueError leak. Symlink-out test (`test_validate_rejects_symlink_pointing_outside`) uses real symlinks in tmp_path, not mocks. Solid.
- Parser's `{`-prefix guard is correct, including the empty-line short-circuit (line 47-48 suppresses noise on blank lines from buffered output). Strict-but-tolerant policy works as documented.
- `extra="allow"` on every model is a deliberate forward-compat choice; pydantic v2 discriminated unions handle the unknown-field-tolerance + known-discriminator-rejection combination cleanly.
- `start_poller` sets default-deny BEFORE returning the task — `test_start_poller_sets_healthy_state` correctly asserts this synchronous contract.
- `_terminate` correctly catches `ProcessLookupError` AND `PermissionError` — the latter is rare but happens under user-namespace remapping in some Docker setups.
- Lifespan shutdown order is correct: poller cancelled BEFORE Redis/DB close (so the poller's last probe doesn't try to log into a closed Redis pool).
- File-size budget honoured: 184 (events), 179 (auth_session), 193 (runner), 118 (workspace), 66 (parser), 27 (exceptions). All under 200.
- Tests cover the SIGTERM-on-timeout path with `os.killpg` patched to a sentinel-list — not bulletproof but adequate.

---

## Recommended Actions (priority order)

1. **C-1 fix** — change `argv.insert(4, "--ephemeral")` to either `argv.insert(5, …)` or `argv.append(…)`. Add a regression test asserting `--color` immediately precedes `never`. **BLOCKING.**
2. **C-2 fix** — reconcile `codex_auth_dir` default with the runner's `HOME` derivation. Either default to `/root/.codex` and keep parent-derivation, OR pass `CODEX_HOME` explicitly. Add an integration test (or smoke) that verifies the spawned subprocess receives a HOME under which `<HOME>/.codex/auth.json` exists. **BLOCKING.**
3. **H-1** — guard `proc.returncode is None` after the suppressed wait; narrow `BaseException` to `Exception`/`CancelledError`.
4. **H-2** — capture `pgid = proc.pid` once at spawn time; reuse instead of `getpgid` calls.
5. **H-3** — change `getattr(..., 'codex_session_healthy', True)` default to `False`.
6. **H-4 + H-5** — tighten auth-session fallback semantics; trust codex's "unhealthy" verdict; either always try both subcommand variants or detect "unknown subcommand" via stderr.
7. **M-3** — replace `assert proc.stdout is not None` with explicit raise.
8. **M-4** — pass `env` and `start_new_session=True` to the auth probe subprocess too.
9. **M-7** — wrap `create_subprocess_exec` call in try/except; re-raise as `CodexRunnerError` for callers.
10. **L-2** — add `assistant_message` + `agent_message` interleaved test for discriminator regression.
11. Document in runner docstring: callers MUST `await gen.aclose()` if they break out of `async for`. (Edge case #6.)
12. Open follow-up TODO: stdin-pipe prompt support for >8 KiB prompts (relevant to phase-03).

---

## Metrics

- Module LOC: 184 / 179 / 193 / 118 / 66 / 27 (all ≤ 200 ✓)
- Test LOC: parser 254, workspace 164, auth_session 205, runner 386, smoke 71
- Test count: ~50 unit tests + 1 integration smoke (skipped without auth)
- Critical issues: 2
- High issues: 5
- Medium issues: 7
- Low issues: 7

---

## Unresolved Questions

1. Does codex 0.125.0 actually accept a `CODEX_HOME` env var or `--config-dir` flag that would let us bypass the HOME derivation in C-2? If yes, that's the cleanest fix.
2. What exit code does `codex auth status` return for "session expired" vs "subcommand not found" vs "auth.json missing"? Without ground truth, H-5's branch logic is guessing. Recommend running `codex auth status` against each scenario manually and pinning behaviour in the auth-session module.
3. Should `codex_has_ephemeral` default flip to `True` once `make verify-codex` confirms the flag, or stay `False` requiring explicit operator opt-in? Current default-False is conservative; spec language suggests it should auto-flip after verify. The wiring is missing — verify-codex.sh must write back into `.env` (or settings file) for runtime to pick it up.

---

**Status:** DONE
**Verdict:** APPROVE_WITH_CHANGES
**Critical count:** 2 (C-1 ephemeral arg-pair split, C-2 HOME derivation under default auth dir)
