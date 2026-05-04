"""
Pytest fixtures for the compat test suite.

Requires docker-compose.test.yml stack to be running (or spun up by the
session-scoped ``compose_stack`` fixture when COMPAT_EXTERNAL_STACK is unset).

Fixture hierarchy:
  compose_stack  (session) — ensures stack is up, yields base_url + admin_token
  test_api_key   (function) — creates a fresh key via admin endpoint, revokes on teardown
  sync_client    (function) — openai.OpenAI configured against the test gateway
  async_client   (function) — openai.AsyncOpenAI for streaming tests

Environment overrides (for running against an already-running stack):
  COMPAT_BASE_URL      default: http://localhost:8001
  COMPAT_ADMIN_TOKEN   default: test-admin-token
  COMPAT_EXTERNAL_STACK=1  — skip compose up/down (use pre-running stack)
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import AsyncGenerator, Generator

import httpx
import pytest
import pytest_asyncio

# ── Constants ─────────────────────────────────────────────────────────────────

_BASE_URL = os.environ.get("COMPAT_BASE_URL", "http://localhost:8001")
_ADMIN_TOKEN = os.environ.get("COMPAT_ADMIN_TOKEN", "test-admin-token")
_EXTERNAL_STACK = os.environ.get("COMPAT_EXTERNAL_STACK", "0") == "1"
_COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.test.yml")
_HEALTHZ_TIMEOUT = 60  # seconds
_HEALTHZ_INTERVAL = 2  # seconds


# ── Stack management ──────────────────────────────────────────────────────────


def _wait_for_healthz(base_url: str, timeout: int = _HEALTHZ_TIMEOUT) -> None:
    """Poll /healthz until 200 or timeout."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=3)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(_HEALTHZ_INTERVAL)
    raise TimeoutError(
        f"Gateway at {base_url}/healthz did not become healthy within {timeout}s. "
        f"Last error: {last_exc}"
    )


def _compose_up() -> None:
    subprocess.run(
        ["docker", "compose", "-f", _COMPOSE_FILE, "up", "-d", "--wait"],
        check=True,
        timeout=120,
    )


def _compose_down() -> None:
    subprocess.run(
        ["docker", "compose", "-f", _COMPOSE_FILE, "down", "-v"],
        check=False,  # don't fail teardown on compose errors
        timeout=60,
    )


# ── Session fixture: compose stack ────────────────────────────────────────────


@pytest.fixture(scope="session")
def compose_stack() -> Generator[tuple[str, str], None, None]:
    """Ensure the test stack is up; yield (base_url, admin_token)."""
    if not _EXTERNAL_STACK:
        _compose_up()

    _wait_for_healthz(_BASE_URL)

    yield _BASE_URL, _ADMIN_TOKEN

    if not _EXTERNAL_STACK:
        _compose_down()


# ── Function fixture: test API key ────────────────────────────────────────────


@pytest.fixture()
def test_api_key(compose_stack: tuple[str, str]) -> Generator[str, None, None]:
    """Create a fresh API key via admin endpoint; revoke on teardown."""
    base_url, admin_token = compose_stack
    headers = {"X-Admin-Token": admin_token, "Content-Type": "application/json"}

    # Create key
    resp = httpx.post(
        f"{base_url}/admin/api-keys",
        json={"user_email": "compat@example.com", "name": "compat-test-key", "tier": "pro"},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    key_id: str = body["id"]
    plaintext: str = body["key"]

    yield plaintext

    # Revoke key
    httpx.delete(
        f"{base_url}/admin/api-keys/{key_id}",
        headers={"X-Admin-Token": admin_token},
        timeout=10,
    )


# ── Function fixture: sync OpenAI client ─────────────────────────────────────


@pytest.fixture()
def sync_client(compose_stack: tuple[str, str], test_api_key: str):  # type: ignore[return]
    """openai.OpenAI pointed at the test gateway."""
    import openai  # noqa: PLC0415 — optional dep, only in compat suite

    base_url, _ = compose_stack
    return openai.OpenAI(
        base_url=f"{base_url}/v1",
        api_key=test_api_key,
        timeout=30,
        max_retries=0,
    )


# ── Function fixture: async OpenAI client ────────────────────────────────────


@pytest_asyncio.fixture()
async def async_client(
    compose_stack: tuple[str, str], test_api_key: str
) -> AsyncGenerator[object, None]:
    """openai.AsyncOpenAI pointed at the test gateway."""
    import openai  # noqa: PLC0415

    base_url, _ = compose_stack
    client = openai.AsyncOpenAI(
        base_url=f"{base_url}/v1",
        api_key=test_api_key,
        timeout=30,
        max_retries=0,
    )
    yield client
    await client.close()


# ── Function fixture: raw httpx client (for byte-level assertions) ────────────


@pytest.fixture()
def raw_http(
    compose_stack: tuple[str, str], test_api_key: str
) -> Generator[httpx.Client, None, None]:
    """Plain httpx.Client for raw SSE byte assertions."""
    base_url, _ = compose_stack
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {test_api_key}"},
        timeout=30,
    ) as client:
        yield client
