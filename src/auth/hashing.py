"""
Argon2id key hashing and plaintext key generation.

All API keys are formatted as:
    cwk_<43 url-safe base64 chars>  →  47 chars total

The prefix "cwk_" (codex-wrapper-key) lets middleware reject non-platform
tokens fast without a DB round-trip.

Argon2id parameters (PasswordHasher defaults):
  time_cost=3, memory_cost=65536 (64 MiB), parallelism=4, hash_len=32, salt_len=16
These are intentionally NOT changed — any change invalidates ALL existing hashes
and requires a full key-rotation migration. Document in a phase if ever changed.

verify_key() returns False on any mismatch — argon2-cffi's PasswordHasher.verify
is constant-time by design, so no timing oracle is possible.
"""

from __future__ import annotations

import base64
import secrets

import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Module-level singleton. Default params: m=64MiB, t=3, p=4, hash_len=32, salt_len=16.
_PH = PasswordHasher()

logger = structlog.get_logger(__name__)

KEY_PREFIX = "cwk_"
_KEY_BYTES = 32  # → ceil(32*4/3) = 43 b64url chars (no padding) → total 47 chars


def generate_api_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns:
        (plaintext, prefix, key_hash) where:
          - plaintext  = "cwk_" + 43 url-safe b64 chars (shown to admin ONCE)
          - prefix     = first 12 chars of plaintext (stored; used for cheap index lookup)
          - key_hash   = argon2id hash of plaintext (stored in DB)
    """
    raw = secrets.token_bytes(_KEY_BYTES)
    plaintext = KEY_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    prefix = plaintext[:12]
    key_hash = _PH.hash(plaintext)
    return plaintext, prefix, key_hash


def verify_key(plaintext: str, key_hash: str) -> bool:
    """Verify a plaintext key against its stored argon2id hash.

    Returns True on a correct match.
    Returns False on mismatch or corrupt/invalid hash (logged as WARNING).
    Lets real failures (MemoryError, TypeError, etc.) propagate — they indicate
    environmental problems that callers must not silently swallow.
    Constant-time by argon2-cffi design.
    """
    try:
        _PH.verify(key_hash, plaintext)
        return True
    except VerifyMismatchError:
        # Expected path: wrong plaintext. No log — high-frequency, not actionable.
        return False
    except (InvalidHashError, VerificationError):
        # Corrupt, malformed, or structurally invalid hash in storage.
        # VerificationError: base class covering "salt too short", bad params, etc.
        # InvalidHashError: hash string does not parse at all (ValueError subclass).
        # Both indicate bad data in the DB row — log WARNING for operational alerting.
        logger.warning("auth.hash.corrupt", hash_prefix=key_hash[:20])
        return False
    # All other exceptions (MemoryError, TypeError, etc.) propagate to callers.
