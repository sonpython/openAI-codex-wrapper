"""
Codex stderr postmortem archive (phase-08 MM6).

For failed jobs (exit_code != 0), persists the full stderr ring-buffer
(capped 64 KiB from runner) so on-call engineers can diagnose failures
after the workspace has been cleaned up.

Storage backends (env-selected):
  local  — write to STDERR_ARCHIVE_LOCAL_DIR (default /var/codex-stderr)
  s3     — write to S3/B2 bucket via boto3 (when STDERR_ARCHIVE_S3_URL set)

Object key: {prefix}/{job_id}.txt  (default prefix: codex-stderr)

Rules:
  - Never fails the job if archive fails — logs WARN and continues.
  - Local mode: creates parent directory on first use.
  - S3 mode: uses boto3 only when STDERR_ARCHIVE_S3_URL is configured.
  - Admin retrieval: GET /admin/codex/jobs/{id}/stderr proxies the object.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from src.settings import get_settings

logger = structlog.get_logger(__name__)

_JOB_ID_RE = re.compile(r"^[0-9a-f\-]{36}$")  # UUID format


def _safe_job_id(job_id: str) -> str:
    """Validate job_id is a UUID string to prevent path traversal."""
    if not _JOB_ID_RE.match(job_id.lower()):
        raise ValueError(f"Invalid job_id for archive: {job_id!r}")
    return job_id.lower()


# ── Local backend ─────────────────────────────────────────────────────────────


def _local_path(job_id: str) -> Path:
    settings = get_settings()
    base = Path(settings.stderr_archive_local_dir)
    return base / f"{job_id}.txt"


def _write_local(job_id: str, content: bytes) -> None:
    path = _local_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    logger.info("stderr_archive.written_local", job_id=job_id, bytes=len(content))


def _read_local(job_id: str) -> bytes | None:
    path = _local_path(job_id)
    if not path.exists():
        return None
    return path.read_bytes()


# ── S3 backend ────────────────────────────────────────────────────────────────


def _write_s3(job_id: str, content: bytes, s3_url: str) -> None:
    """Write stderr content to S3/B2 bucket."""
    try:
        import boto3  # noqa: PLC0415

        # Parse s3://bucket-name/optional-prefix
        without_scheme = s3_url.removeprefix("s3://").removeprefix("https://")
        parts = without_scheme.split("/", 1)
        bucket = parts[0]
        key_prefix = parts[1].rstrip("/") if len(parts) > 1 else "codex-stderr"
        key = f"{key_prefix}/{job_id}.txt"

        s3 = boto3.client("s3")
        s3.put_object(Bucket=bucket, Key=key, Body=content, ContentType="text/plain")
        logger.info("stderr_archive.written_s3", job_id=job_id, bucket=bucket, key=key)
    except Exception:
        logger.warning("stderr_archive.s3_write_failed", job_id=job_id, exc_info=True)
        raise


def _read_s3(job_id: str, s3_url: str) -> bytes | None:
    """Read stderr content from S3/B2 bucket."""
    try:
        import boto3  # noqa: PLC0415

        without_scheme = s3_url.removeprefix("s3://").removeprefix("https://")
        parts = without_scheme.split("/", 1)
        bucket = parts[0]
        key_prefix = parts[1].rstrip("/") if len(parts) > 1 else "codex-stderr"
        key = f"{key_prefix}/{job_id}.txt"

        s3 = boto3.client("s3")
        response = s3.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()  # type: ignore[no-any-return]
    except Exception:
        logger.warning("stderr_archive.s3_read_failed", job_id=job_id, exc_info=True)
        return None


# ── Public API ────────────────────────────────────────────────────────────────


def archive_stderr(job_id: str, stderr_content: bytes) -> None:
    """Persist stderr for a failed job. Never raises — logs WARN on failure.

    Called from job_handlers.py failure path before workspace cleanup.
    """
    if not stderr_content:
        return

    try:
        safe_id = _safe_job_id(job_id)
    except ValueError:
        logger.warning("stderr_archive.invalid_job_id", job_id=job_id)
        return

    settings = get_settings()

    try:
        if settings.stderr_archive_s3_url:
            _write_s3(safe_id, stderr_content, settings.stderr_archive_s3_url)
        else:
            _write_local(safe_id, stderr_content)
    except Exception:
        # MM6: never fail the job because archive failed
        logger.warning("stderr_archive.write_failed", job_id=safe_id, exc_info=True)


def retrieve_stderr(job_id: str) -> bytes | None:
    """Read archived stderr for a job. Returns None if not found.

    Used by GET /admin/codex/jobs/{id}/stderr endpoint.
    """
    try:
        safe_id = _safe_job_id(job_id)
    except ValueError:
        return None

    settings = get_settings()

    if settings.stderr_archive_s3_url:
        return _read_s3(safe_id, settings.stderr_archive_s3_url)
    return _read_local(safe_id)
