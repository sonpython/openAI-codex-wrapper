"""
Admin endpoint: retrieve archived stderr for a failed codex job.

  GET /admin/codex/jobs/{job_id}/stderr

Auth: X-Admin-Token (same dependency as admin_api_keys.py).
Returns the archived stderr bytes as text/plain.
404 if no archive exists (job succeeded, or archive was not written).

Audit-logged with action="stderr_retrieve".
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse

from src.codex.stderr_archive import retrieve_stderr
from src.settings import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])


def _verify_admin_token(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """Constant-time admin token check (duplicated from admin_api_keys for locality)."""
    import secrets  # noqa: PLC0415

    settings = get_settings()
    expected = settings.admin_token.get_secret_value()
    if x_admin_token is None or not secrets.compare_digest(
        x_admin_token.encode(), expected.encode()
    ):
        raise HTTPException(status_code=403, detail="permission_denied")


AdminTokenDep = Annotated[None, Depends(_verify_admin_token)]


@router.get("/admin/codex/jobs/{job_id}/stderr", response_class=PlainTextResponse)
async def get_job_stderr(
    job_id: str,
    _: AdminTokenDep,
) -> PlainTextResponse:
    """Return archived stderr for a failed job as plain text.

    Returns 404 if no stderr archive exists (job succeeded or archive failed).
    Requires X-Admin-Token header.
    """
    content = retrieve_stderr(job_id)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail="No stderr archive found for this job. "
            "Job may have succeeded or stderr was not captured.",
        )

    logger.info("admin.stderr_retrieved", job_id=job_id, bytes=len(content))

    return PlainTextResponse(
        content=content.decode("utf-8", errors="replace"),
        media_type="text/plain",
    )
