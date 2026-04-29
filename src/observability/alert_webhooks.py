"""
Alert webhook helper — phase-08.

Sends structured alerts to an HTTP/Slack endpoint when critical events occur
(e.g. Codex session goes unhealthy). Best-effort: all errors are swallowed
and logged as WARN so webhook failure never breaks the caller.

Configuration (settings):
  WEBHOOK_ALERT_URL   — full URL to POST alerts to (None = disabled)
  WEBHOOK_ALERT_KIND  — "slack" | "http" (default "http")

Slack payload shape: {"text": "<severity> <message>", "attachments": [...]}
HTTP payload shape:  {"severity": ..., "message": ..., "fields": {...}}
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def send_alert(
    severity: str,
    message: str,
    fields: dict[str, Any] | None = None,
) -> None:
    """POST an alert to the configured webhook URL.

    Args:
        severity: "critical" | "warning" | "info"
        message:  Human-readable alert text.
        fields:   Optional structured context (e.g. job_id, expires_at).

    Never raises — all exceptions are caught and logged at WARN level.
    No-ops when WEBHOOK_ALERT_URL is not set.
    """
    from src.settings import get_settings  # noqa: PLC0415

    settings = get_settings()
    url = settings.webhook_alert_url
    if not url:
        return

    kind = settings.webhook_alert_kind
    payload = _build_payload(severity, message, fields or {}, kind)

    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "alert_webhook.non_2xx",
                    status=resp.status_code,
                    url=url,
                )
    except Exception:
        logger.warning("alert_webhook.send_failed", url=url, exc_info=True)


def _build_payload(
    severity: str,
    message: str,
    fields: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    if kind == "slack":
        text = f"[{severity.upper()}] {message}"
        attachment_fields = [
            {"title": k, "value": str(v), "short": True} for k, v in fields.items()
        ]
        return {
            "text": text,
            "attachments": [{"color": _slack_color(severity), "fields": attachment_fields}],
        }
    # Default: generic HTTP JSON
    return {
        "severity": severity,
        "message": message,
        "fields": fields,
    }


def _slack_color(severity: str) -> str:
    return {"critical": "danger", "warning": "warning", "info": "good"}.get(severity, "#cccccc")
