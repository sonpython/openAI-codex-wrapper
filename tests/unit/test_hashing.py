"""
Unit tests for src/auth/hashing.py.

Covers:
  - generate_api_key() format invariants
  - generate_api_key() uniqueness across N=100 calls
  - verify_key() happy path (correct plaintext verifies)
  - verify_key() mismatch path (wrong plaintext returns False)
  - verify_key() corrupted hash returns False (no exception)
  - prefix extraction (first 12 chars)
"""

from __future__ import annotations

from src.auth.hashing import KEY_PREFIX, generate_api_key, verify_key


def test_generate_api_key_starts_with_prefix() -> None:
    plaintext, prefix, key_hash = generate_api_key()
    assert plaintext.startswith(KEY_PREFIX), f"expected 'cwk_' prefix, got {plaintext[:8]!r}"


def test_generate_api_key_length() -> None:
    """cwk_ (4) + 43 b64url chars = 47 total."""
    plaintext, _, _ = generate_api_key()
    assert len(plaintext) == 47, f"expected length 47, got {len(plaintext)}"


def test_generate_api_key_prefix_is_first_12_chars() -> None:
    plaintext, prefix, _ = generate_api_key()
    assert prefix == plaintext[:12]


def test_generate_api_key_hash_is_argon2id() -> None:
    _, _, key_hash = generate_api_key()
    # argon2-cffi always produces hashes starting with $argon2id$
    assert key_hash.startswith("$argon2id$"), f"unexpected hash format: {key_hash[:20]!r}"


def test_generate_api_key_uniqueness() -> None:
    """100 generated keys must all be distinct (plaintexts and hashes)."""
    keys = [generate_api_key() for _ in range(100)]
    plaintexts = [k[0] for k in keys]
    hashes = [k[2] for k in keys]
    assert len(set(plaintexts)) == 100, "plaintext collision detected in 100 keys"
    assert len(set(hashes)) == 100, "hash collision detected (different salts expected)"


def test_verify_key_correct_plaintext() -> None:
    plaintext, _, key_hash = generate_api_key()
    assert verify_key(plaintext, key_hash) is True


def test_verify_key_wrong_plaintext() -> None:
    plaintext, _, key_hash = generate_api_key()
    # Flip one character
    wrong = plaintext[:-1] + ("X" if plaintext[-1] != "X" else "Y")
    assert verify_key(wrong, key_hash) is False


def test_verify_key_different_key_same_hash_format() -> None:
    plaintext1, _, hash1 = generate_api_key()
    plaintext2, _, _ = generate_api_key()
    assert plaintext1 != plaintext2  # sanity
    assert verify_key(plaintext2, hash1) is False


def test_verify_key_corrupted_hash_returns_false() -> None:
    plaintext, _, _ = generate_api_key()
    corrupted = "not-a-valid-argon2-hash"
    # Must return False, never raise
    assert verify_key(plaintext, corrupted) is False


def test_verify_key_empty_string_returns_false() -> None:
    assert verify_key("", "$argon2id$v=19$m=65536,t=3,p=4$fakesalt$fakehash") is False


def test_verify_key_same_plaintext_different_salts_both_pass() -> None:
    """Two independently generated hashes of the same plaintext both verify."""
    from argon2 import PasswordHasher  # noqa: PLC0415

    ph = PasswordHasher()
    plaintext = "cwk_testonlyplaintextvalue000000000000000000000"
    hash_a = ph.hash(plaintext)
    hash_b = ph.hash(plaintext)
    # Salts differ → hashes differ
    assert hash_a != hash_b
    # Both must verify
    assert verify_key(plaintext, hash_a) is True
    assert verify_key(plaintext, hash_b) is True
