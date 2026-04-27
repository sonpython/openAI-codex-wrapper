"""
Pydantic request/response schemas for the /v1/codex/jobs endpoints.

Validation rules:
  - repo_url: public GitHub HTTPS only (regex-enforced); SSH/private rejected.
  - branch: alphanumeric + ._/- up to 200 chars.
  - task: non-empty, max 100KB (8000 chars per phase spec).
  - run_tests: reserved; True rejected with explicit v1.1 error.
  - mode: "read-only" | "workspace-write" literal.
  - timeout_seconds: optional, 1–3600.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# Public GitHub HTTPS only: https://github.com/owner/repo[.git][/]
_GITHUB_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:\.git)?/?$")
# H4: Tightened — rejects leading '-', '..' path-traversal segments, and '//' consecutive slashes.
_BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}$")

# 1 MB cap for diff_blob in API responses (full blob may be larger in DB).
DIFF_RESPONSE_MAX_BYTES = 1_048_576


class JobCreateRequest(BaseModel):
    """Request body for POST /v1/codex/jobs."""

    repo_url: str
    branch: str = "main"
    task: str
    mode: Literal["read-only", "workspace-write"] = "read-only"
    run_tests: bool = False
    timeout_seconds: int | None = Field(None, gt=0, le=3600)

    @field_validator("repo_url")
    @classmethod
    def validate_public_github_url(cls, v: str) -> str:
        if not _GITHUB_URL_RE.match(v):
            raise ValueError(
                "repo_url must be a public GitHub HTTPS URL "
                "(e.g. https://github.com/owner/repo). "
                "SSH URLs, private repos, and non-GitHub hosts are not supported in v1."
            )
        return v

    @field_validator("branch")
    @classmethod
    def validate_branch_name(cls, v: str) -> str:
        if not _BRANCH_RE.match(v):
            raise ValueError(
                "branch must contain only alphanumeric characters, '.', '_', '/', '-', "
                "must not start with '-', must not contain '..' or '//' sequences, "
                "and must be 1–200 characters long."
            )
        return v

    @field_validator("task")
    @classmethod
    def validate_task_non_empty_and_length(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task must not be empty.")
        if len(v) > 100_000:
            raise ValueError("task exceeds maximum length of 100,000 characters.")
        return v

    @field_validator("run_tests")
    @classmethod
    def reject_run_tests(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "run_tests is not supported in v1. " "This feature is planned for v1.1."
            )
        return v


class JobCreatedResponse(BaseModel):
    """Response body for POST /v1/codex/jobs (202 Accepted)."""

    id: UUID
    status: str = "queued"
    created_at: datetime


class JobResponse(BaseModel):
    """Full job state — response body for GET /v1/codex/jobs/{id}."""

    id: UUID
    status: str
    repo_url: str
    branch: str
    task: str
    mode: str
    summary: str | None
    # diff_blob truncated to 1MB in responses; diff_truncated=True signals more.
    diff_blob: str | None
    diff_truncated: bool
    diff_size_bytes: int | None
    files_changed: list[str] | None
    exit_code: int | None
    error_code: str | None
    error_message: str | None
    enqueued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_job(cls, job: Any) -> JobResponse:
        """Build JobResponse from a Job ORM instance, truncating diff if needed."""
        raw_diff = job.diff_blob
        truncated = False
        if raw_diff and len(raw_diff.encode()) > DIFF_RESPONSE_MAX_BYTES:
            # Truncate to exactly DIFF_RESPONSE_MAX_BYTES worth of UTF-8 bytes.
            raw_diff = raw_diff.encode()[:DIFF_RESPONSE_MAX_BYTES].decode("utf-8", errors="ignore")
            truncated = True

        return cls(
            id=job.id,
            status=job.status,
            repo_url=job.repo_url,
            branch=job.branch,
            task=job.task,
            mode=job.mode,
            summary=job.summary,
            diff_blob=raw_diff,
            diff_truncated=truncated,
            diff_size_bytes=job.diff_size_bytes,
            files_changed=job.files_changed,
            exit_code=job.exit_code,
            error_code=job.error_code,
            error_message=job.error_message,
            enqueued_at=job.enqueued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
