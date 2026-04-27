"""
Tests for codex stderr archive (phase-08 MM6).

Covers:
  - archive_stderr writes file to local dir
  - retrieve_stderr reads it back
  - invalid job_id silently ignored (no raise)
  - retrieve_stderr returns None when file absent
  - archive_stderr never raises even on OS error
  - Admin endpoint GET /admin/codex/jobs/{id}/stderr returns content
  - Admin endpoint returns 401 without token
  - Admin endpoint returns 404 when no archive
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.codex.stderr_archive import archive_stderr, retrieve_stderr
from src.gateway.routes.admin_codex_stderr import router

# ── archive_stderr / retrieve_stderr unit tests ───────────────────────────────


def test_archive_and_retrieve_local(tmp_path, monkeypatch):
    """Write then read back works in local mode."""
    job_id = "12345678-1234-1234-1234-123456789abc"
    content = b"boom: segfault at line 42"

    mock_settings = _mock_settings(str(tmp_path))
    monkeypatch.setattr("src.codex.stderr_archive.get_settings", lambda: mock_settings)

    archive_stderr(job_id, content)
    result = retrieve_stderr(job_id)

    assert result == content


def test_retrieve_returns_none_when_missing(tmp_path, monkeypatch):
    mock_settings = _mock_settings(str(tmp_path))
    monkeypatch.setattr("src.codex.stderr_archive.get_settings", lambda: mock_settings)

    result = retrieve_stderr("99999999-0000-0000-0000-000000000000")
    assert result is None


def test_archive_stderr_invalid_job_id_no_raise(monkeypatch):
    """Invalid job_id (path traversal attempt) is silently ignored."""
    mock_settings = _mock_settings("/tmp")
    monkeypatch.setattr("src.codex.stderr_archive.get_settings", lambda: mock_settings)

    # Must not raise
    archive_stderr("../../etc/passwd", b"evil content")


def test_archive_stderr_swallows_os_error(tmp_path, monkeypatch):
    """OS error during write is swallowed (job must not fail)."""
    job_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    mock_settings = _mock_settings("/proc/nonexistent/path")  # guaranteed to fail
    monkeypatch.setattr("src.codex.stderr_archive.get_settings", lambda: mock_settings)

    # Must not raise
    archive_stderr(job_id, b"some stderr")


def test_archive_stderr_skips_empty_content(tmp_path, monkeypatch):
    """Empty stderr bytes → no file written."""
    job_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    mock_settings = _mock_settings(str(tmp_path))
    monkeypatch.setattr("src.codex.stderr_archive.get_settings", lambda: mock_settings)

    archive_stderr(job_id, b"")

    expected = tmp_path / f"{job_id}.txt"
    assert not expected.exists()


# ── Admin endpoint tests ──────────────────────────────────────────────────────


@pytest.fixture()
def stderr_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_admin_stderr_returns_content(stderr_client, tmp_path, monkeypatch):
    job_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    content = b"fatal error: null pointer"

    mock_settings = _mock_settings(str(tmp_path))
    monkeypatch.setattr("src.codex.stderr_archive.get_settings", lambda: mock_settings)
    monkeypatch.setattr("src.gateway.routes.admin_codex_stderr.get_settings", lambda: mock_settings)

    # Write archive directly
    (tmp_path / f"{job_id}.txt").write_bytes(content)

    with patch("src.gateway.routes.admin_codex_stderr.retrieve_stderr", return_value=content):
        resp = stderr_client.get(
            f"/admin/codex/jobs/{job_id}/stderr",
            headers={"X-Admin-Token": "dev-admin-token-replace-in-prod"},
        )

    assert resp.status_code == 200
    assert b"fatal error" in resp.content


def test_admin_stderr_401_no_token(stderr_client):
    resp = stderr_client.get("/admin/codex/jobs/some-id/stderr")
    assert resp.status_code == 403


def test_admin_stderr_404_no_archive(stderr_client, monkeypatch):
    mock_settings = _mock_settings("/tmp/no-such-dir")
    monkeypatch.setattr("src.gateway.routes.admin_codex_stderr.get_settings", lambda: mock_settings)

    with patch("src.gateway.routes.admin_codex_stderr.retrieve_stderr", return_value=None):
        resp = stderr_client.get(
            "/admin/codex/jobs/dddddddd-dddd-dddd-dddd-dddddddddddd/stderr",
            headers={"X-Admin-Token": "dev-admin-token-replace-in-prod"},
        )

    assert resp.status_code == 404


# ── Helper ────────────────────────────────────────────────────────────────────


def _mock_settings(local_dir: str):
    from unittest.mock import MagicMock

    s = MagicMock()
    s.stderr_archive_local_dir = local_dir
    s.stderr_archive_s3_url = None
    s.admin_token.get_secret_value.return_value = "dev-admin-token-replace-in-prod"
    return s
