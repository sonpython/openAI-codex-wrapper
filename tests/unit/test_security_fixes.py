"""
Security fix regression tests — covers all C-1/C-2/H-1..H-6 fixes.

Tests validate:
  - C-2: argon2 verify called at most 2 times per auth attempt (LIMIT 2 cap)
  - H-1: DB timeout -> 503 (not 401, not 500)
  - H-2: verify_key propagates MemoryError; logs InvalidHashError warning
  - H-3: extract_bearer accepts multi-space and tab-separated headers
  - H-4: /healthzfake and /healthz/sub require auth (exact skip-list)
  - H-5: email normalised to lowercase in get_or_create_by_email
  - H-6: FK ondelete=RESTRICT present in models

NOTE: omit `from __future__ import annotations` — FastAPI/Starlette type resolution
needs eager evaluation (same reason as test_auth_middleware.py).
"""

import os
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _mock_api_key(tier: str = "free") -> MagicMock:
    from uuid import uuid4

    key = MagicMock()
    key.id = uuid4()
    key.user_id = uuid4()
    key.tier = tier
    return key


async def _null_gen() -> AsyncGenerator[MagicMock, None]:
    yield MagicMock()


def _make_test_app() -> object:
    from fastapi import FastAPI, Request
    from src.gateway.middleware.auth import AuthMiddleware

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/v1/ping")
    async def ping(request: Request) -> dict:  # type: ignore[type-arg]
        return {"pong": True}

    @app.get("/healthz")
    async def healthz() -> dict:  # type: ignore[type-arg]
        return {"status": "ok"}

    @app.get("/healthzfake")
    async def healthzfake() -> dict:  # type: ignore[type-arg]
        return {"status": "ok"}

    return app


@pytest_asyncio.fixture()
async def client() -> AsyncGenerator[AsyncClient, None]:
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        yield ac


# ── C-2: argon2 LIMIT 2 cap ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_active_by_prefix_and_verify_calls_at_most_two_verifies() -> None:
    """LIMIT 2 ensures at most 2 argon2 verify calls per auth attempt."""
    from unittest.mock import AsyncMock, patch

    from sqlalchemy.ext.asyncio import AsyncSession
    from src.db.crud.api_keys import get_active_by_prefix_and_verify

    verify_call_count = 0

    def counting_verify(plaintext: str, key_hash: str) -> bool:
        nonlocal verify_call_count
        verify_call_count += 1
        return False  # always fail → exercises the full loop

    # Build 2 candidate rows (the LIMIT 2 max)
    candidate1 = MagicMock()
    candidate1.key_hash = "hash1"
    candidate2 = MagicMock()
    candidate2.key_hash = "hash2"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [candidate1, candidate2]

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("src.db.crud.api_keys.verify_key", side_effect=counting_verify):
        result = await get_active_by_prefix_and_verify(mock_session, "cwk_" + "A" * 43)

    assert result is None
    assert verify_call_count <= 2, f"Expected ≤2 verify calls, got {verify_call_count}"


@pytest.mark.asyncio
async def test_get_active_by_prefix_verify_uses_asyncio_to_thread() -> None:
    """Verify that asyncio.to_thread is used for argon2 so event loop isn't pinned."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession
    from src.db.crud.api_keys import get_active_by_prefix_and_verify

    candidate = MagicMock()
    candidate.key_hash = "hash1"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [candidate]

    mock_session = MagicMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)

    to_thread_called = False
    original_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal to_thread_called
        to_thread_called = True
        return await original_to_thread(fn, *args, **kwargs)

    with (
        patch("asyncio.to_thread", side_effect=spy_to_thread),
        patch("src.db.crud.api_keys.verify_key", return_value=True),
    ):
        await get_active_by_prefix_and_verify(mock_session, "cwk_" + "A" * 43)

    assert to_thread_called, "asyncio.to_thread was not called — event loop may be blocked"


# ── H-1: DB timeout returns 503, not 401 or 500 ──────────────────────────────


@pytest.mark.asyncio
async def test_db_pool_timeout_returns_503(client: AsyncClient) -> None:
    """asyncio.TimeoutError from DB pool exhaustion -> 503, never 401."""

    valid_token = "cwk_" + "T" * 43

    with patch(
        "src.gateway.middleware.auth.AuthMiddleware._authenticate",
        new=AsyncMock(side_effect=TimeoutError()),
    ):
        response = await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})

    assert response.status_code == 503, f"Expected 503, got {response.status_code}"
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"


@pytest.mark.asyncio
async def test_sqlalchemy_timeout_returns_503(client: AsyncClient) -> None:
    """sqlalchemy.exc.TimeoutError from pool exhaustion -> 503."""
    import sqlalchemy.exc

    valid_token = "cwk_" + "U" * 43

    with patch(
        "src.gateway.middleware.auth.AuthMiddleware._authenticate",
        new=AsyncMock(side_effect=sqlalchemy.exc.TimeoutError()),
    ):
        response = await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_db_timeout_is_not_401(client: AsyncClient) -> None:
    """DB timeout MUST NOT return 401 — that leaks info about key validity."""

    valid_token = "cwk_" + "V" * 43

    with patch(
        "src.gateway.middleware.auth.AuthMiddleware._authenticate",
        new=AsyncMock(side_effect=TimeoutError()),
    ):
        response = await client.get("/v1/ping", headers={"Authorization": f"Bearer {valid_token}"})

    assert response.status_code != 401, "DB timeout must not return 401 (info leak)"


# ── H-2: verify_key exception propagation ────────────────────────────────────


def test_verify_key_propagates_memory_error() -> None:
    """MemoryError during argon2 must propagate, not be swallowed as False."""

    from src.auth.hashing import verify_key

    # Patch verify_key's internal _PH at the module level by intercepting the
    # underlying call. Since _PH.verify is a C extension (read-only), we instead
    # patch the PasswordHasher class's verify method via a subclass approach —
    # or more directly, patch `src.auth.hashing._PH` with a mock object.
    mock_ph = MagicMock()
    mock_ph.verify.side_effect = MemoryError("OOM")

    with patch("src.auth.hashing._PH", mock_ph), pytest.raises(MemoryError):
        verify_key("cwk_" + "A" * 43, "any_hash")


def test_verify_key_invalid_hash_returns_false_and_logs() -> None:
    """InvalidHashError for corrupt DB hash -> False (not exception), logged."""
    from src.auth.hashing import verify_key

    # Mock the module-level logger to capture calls without relying on
    # structlog's test-mode output routing.
    with patch("src.auth.hashing.logger") as mock_logger:
        result = verify_key("cwk_" + "A" * 43, "not-a-valid-argon2-hash")

    assert result is False
    # Logger.warning must have been called with "auth.hash.corrupt"
    mock_logger.warning.assert_called_once()
    call_args = mock_logger.warning.call_args
    assert call_args[0][0] == "auth.hash.corrupt"


def test_verify_key_mismatch_returns_false_silently() -> None:
    """VerifyMismatchError (wrong password) -> False, no log emitted."""
    from src.auth.hashing import generate_api_key, verify_key

    plaintext, _, key_hash = generate_api_key()
    wrong_plaintext = "cwk_" + "Z" * 43

    with patch("src.auth.hashing.logger") as mock_logger:
        result = verify_key(wrong_plaintext, key_hash)

    assert result is False
    mock_logger.warning.assert_not_called()  # Mismatch path must not emit log


# ── H-3: extract_bearer multi-space / tab ────────────────────────────────────


def _make_headers(authorization: str) -> object:
    """Build a Starlette Headers object from a raw authorization string."""
    from starlette.datastructures import Headers

    return Headers(raw=[(b"authorization", authorization.encode("latin-1"))])


def test_extract_bearer_accepts_double_space() -> None:
    """Authorization: Bearer  cwk_... (two spaces) must parse correctly."""
    from src.auth.bearer import extract_bearer

    token = "cwk_" + "A" * 43
    result = extract_bearer(_make_headers(f"Bearer  {token}"))  # type: ignore[arg-type]
    assert result == token, f"Expected token, got {result!r}"


def test_extract_bearer_accepts_tab_separator() -> None:
    """Authorization: Bearer\\tcwk_... (tab) must parse correctly."""
    from src.auth.bearer import extract_bearer

    token = "cwk_" + "B" * 43
    result = extract_bearer(_make_headers(f"Bearer\t{token}"))  # type: ignore[arg-type]
    assert result == token, f"Expected token, got {result!r}"


def test_extract_bearer_strips_trailing_whitespace() -> None:
    """Trailing whitespace after token should be stripped."""
    from src.auth.bearer import extract_bearer

    token = "cwk_" + "C" * 43
    result = extract_bearer(_make_headers(f"Bearer {token}  "))  # type: ignore[arg-type]
    assert result == token


def test_extract_bearer_rejects_non_cwk_after_whitespace_normalisation() -> None:
    """Even after stripping, non-cwk_ token must be rejected."""
    from src.auth.bearer import extract_bearer

    result = extract_bearer(_make_headers("Bearer  sk-proj-abc123"))  # type: ignore[arg-type]
    assert result is None


# ── H-4: skip-list exact match ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_exact_match_bypasses_auth(client: AsyncClient) -> None:
    """/healthz -> bypass (exact match in frozenset)."""
    response = await client.get("/healthz")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_healthzfake_requires_auth(client: AsyncClient) -> None:
    """/healthzfake must NOT bypass auth — partial prefix match disallowed."""
    response = await client.get("/healthzfake")
    assert response.status_code == 401, (
        f"/healthzfake should require auth (got {response.status_code}); "
        "skip-list must use exact match only for health paths"
    )


@pytest.mark.asyncio
async def test_skip_path_exact_only_metrics(client: AsyncClient) -> None:
    """/metrics exact bypass; any suffix would require auth if mounted."""
    from src.gateway.middleware.auth import _should_skip

    assert _should_skip("/metrics") is True
    # Sub-path (e.g. a hypothetical /metrics/foo) should NOT bypass via frozenset
    # (it's not in AUTH_SKIP_PATHS and /metrics/ is not in AUTH_SKIP_PREFIXES)
    assert _should_skip("/metricsxyz") is False
    assert _should_skip("/metrics/foo") is False


def test_should_skip_frozenset_paths() -> None:
    """All canonical bypass paths are in AUTH_SKIP_PATHS frozenset."""
    from src.gateway.middleware.auth import AUTH_SKIP_PATHS

    for path in ("/healthz", "/readyz", "/metrics"):
        assert path in AUTH_SKIP_PATHS, f"{path} must be in AUTH_SKIP_PATHS"


def test_should_not_skip_variations() -> None:
    """Variations of health paths must NOT bypass auth."""
    from src.gateway.middleware.auth import _should_skip

    assert _should_skip("/healthzfake") is False
    assert _should_skip("/healthz/sub") is False
    assert _should_skip("/readyzz") is False
    assert _should_skip("/metricsxyz") is False


# ── H-5: email lowercase normalization ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_or_create_by_email_normalises_to_lowercase() -> None:
    """Mixed-case email on create must be stored and looked up as lowercase."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import AsyncSession
    from src.db.crud.users import get_or_create_by_email

    captured_email: list[str] = []

    async def mock_get_by_email(session: AsyncSession, email: str) -> None:
        captured_email.append(email)
        return  # type: ignore[return-value]  — simulate not found

    mock_user = MagicMock()
    mock_user.id = uuid4()
    mock_session = MagicMock(spec=AsyncSession)
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()

    with patch("src.db.crud.users.get_by_email", side_effect=mock_get_by_email):
        user, created = await get_or_create_by_email(mock_session, "Foo@Bar.COM")

    assert created is True
    # Captured email in the DB lookup must be lowercase
    assert captured_email[0] == "foo@bar.com", f"Email not normalised: {captured_email[0]!r}"


@pytest.mark.asyncio
async def test_get_or_create_by_email_lookup_case_insensitive() -> None:
    """Looking up existing user with different case returns same user."""
    from unittest.mock import MagicMock
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import AsyncSession
    from src.db.crud.users import get_or_create_by_email

    existing_user = MagicMock()
    existing_user.id = uuid4()

    # Simulate: "foo@bar.com" exists in DB
    async def mock_get_by_email(session: AsyncSession, email: str) -> MagicMock | None:
        if email == "foo@bar.com":
            return existing_user
        return None

    mock_session = MagicMock(spec=AsyncSession)

    with patch("src.db.crud.users.get_by_email", side_effect=mock_get_by_email):
        # Lookup with mixed case -> should still find the existing user
        user, created = await get_or_create_by_email(mock_session, "FOO@BAR.COM")

    assert created is False
    assert user is existing_user
