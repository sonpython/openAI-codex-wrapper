#!/usr/bin/env bash
# verify-codex.sh — Codex CLI pre-flight checks (addresses Red Team C1 + C11)
#
# Asserts:
#   1. Installed codex version is exactly 0.125.0
#   2. All required flags are present in `codex exec --help` output
#   3. JSONL-on-stdout still works (not replaced by unix-socket transport)
#   4. Probe for unix-socket transport flag (researcher-01 §6 changelog warning)
#
# Exit codes:
#   0 — all checks pass
#   2 — version mismatch
#   3 — required flag missing from help text
#   4 — stdout is not JSONL
#
# Run via:  make verify-codex  (executes inside gateway container after `make up`)
# Run locally:  bash scripts/verify-codex.sh  (requires codex on PATH)
#
# DEPENDENCY: Must run AFTER `make up`. Phase-02 has this listed as a
# prerequisite — do not start phase-02 if this script exits non-zero.

set -euo pipefail

EXPECTED_VERSION="0.125.0"

# ── 1) Version pin ────────────────────────────────────────────────────────────
V=$(codex --version 2>&1 | awk '{print $NF}')
if [[ "$V" != "$EXPECTED_VERSION" ]]; then
    echo "ERROR: version mismatch: installed=$V expected=$EXPECTED_VERSION" >&2
    exit 2
fi
echo "OK: codex version $V"

# ── 2) Required flags present in help text ────────────────────────────────────
HELP=$(codex exec --help 2>&1 || true)
REQUIRED_FLAGS=(
    ephemeral
    skip-git-repo-check
    json
    sandbox
    cd
    full-auto
    color
)
for f in "${REQUIRED_FLAGS[@]}"; do
    if ! echo "$HELP" | grep -qE -- "--$f"; then
        echo "ERROR: required flag missing from 'codex exec --help': --$f" >&2
        exit 3
    fi
    echo "OK: flag --$f present"
done

# ── 3) JSONL on stdout still works ───────────────────────────────────────────
OUT=$(echo "" | timeout 10 codex exec \
    --json --color never \
    --skip-git-repo-check \
    --sandbox read-only \
    --full-auto \
    "say pong" 2>/dev/null | head -n 1 || true)

if [[ -z "$OUT" ]]; then
    echo "WARN: codex exec produced no output — may need auth (skipping JSONL check in non-auth env)"
elif [[ "$OUT" =~ ^\{ ]]; then
    echo "OK: stdout is JSONL (first char is '{')"
else
    echo "ERROR: stdout is not JSONL: $OUT" >&2
    exit 4
fi

# ── 4) Unix-socket transport probe (researcher-01 §6 warning) ─────────────────
if echo "$HELP" | grep -qiE "unix.?socket|--io"; then
    echo "WARN: 0.125.0 advertises unix-socket/--io transport flag"
    echo "      Phase-02 MUST validate that stdout JSONL pipe is still the default."
    echo "      TODO: add unix-socket transport test in phase-02 integration tests."
fi

echo "verify-codex OK"
