"""
Application settings loaded from environment variables via pydantic-settings.

All configuration is centralised here. Import `get_settings()` everywhere;
never read `os.environ` directly in application code.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for codex-wrapper gateway and worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Extra fields from .env are ignored — prevents surprise failures on
        # env vars added by Docker/CI that are not part of our schema.
        extra="ignore",
    )

    # ── Environment ────────────────────────────────────────────────────────
    wrapper_env: str = "dev"  # dev | staging | prod

    # ── Database ───────────────────────────────────────────────────────────
    # Must be postgresql+asyncpg://user:pass@host:port/db
    database_url: str

    # Pool sizing — see src/db/engine.py for the capacity math.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: float = 2.0

    # Separate, smaller pool for fire-and-forget background writes.
    # On acquire timeout the write is DROPPED (best-effort).
    bg_db_pool_size: int = 3
    bg_db_pool_timeout: float = 0.5

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_url: str  # e.g. redis://redis:6379/0

    # ── Codex CLI ─────────────────────────────────────────────────────────
    codex_bin: str = "codex"  # Path or name of the codex executable
    codex_auth_dir: str = "/codex-auth"  # RO bind-mount of ~/.codex

    # ── Codex feature flags ────────────────────────────────────────────────
    # Set codex_has_ephemeral=True only after ``make verify-codex`` confirms
    # --ephemeral exists in the pinned codex version (C1 fix). Default False
    # means runner uses --cd only; no session-persistence claim.
    codex_has_ephemeral: bool = False
    # Interval (seconds) between background auth-session health probes.
    codex_session_poll_interval_seconds: int = 300
    # Timeout (seconds) for ``codex auth status`` subprocess probe.
    codex_auth_probe_timeout_seconds: int = 3

    # ── Job lifecycle ──────────────────────────────────────────────────────
    workspace_root: str = "/workspaces"  # Parent dir for ephemeral job dirs
    job_timeout_seconds: int = 900  # 15 min default
    job_cancel_grace_seconds: int = 5  # SIGTERM → SIGKILL window
    job_default_timeout_seconds: int = 900  # default when request omits timeout_seconds
    job_clone_timeout_seconds: int = 60  # git clone subprocess timeout
    job_diff_max_bytes: int = 1_048_576  # 1MB cap on diff_blob in API responses

    # ── Arq worker ─────────────────────────────────────────────────────────
    arq_max_jobs: int = 4  # max concurrent jobs per worker process
    # Defaults to redis_url if unset — allows separate Redis for job queue.
    arq_redis_url: str | None = None

    # ── Chat completions ───────────────────────────────────────────────────
    # Default timeout for a single chat completion request (sync or stream).
    chat_default_timeout_seconds: int = 120
    # Maximum assembled prompt length in characters — guards against runaway
    # prompts before they reach the subprocess. Checked in build_prompt().
    chat_max_prompt_chars: int = 200_000

    # ── Responses API (/v1/responses) ─────────────────────────────────────
    # Timeout for a single Responses API request (sync or stream).
    responses_timeout_seconds: int = 120
    # Text-delta chunker window (chars). Codex emits agent_message in one
    # shot; we chunk it to simulate streaming deltas. Phase-08 may improve
    # granularity via tiktoken.
    responses_chunk_chars: int = 50
    # Maximum assembled input length (instructions + input) in characters.
    responses_max_input_chars: int = 200_000

    # ── Rate limiting ──────────────────────────────────────────────────────
    # Bypass all rate-limit checks (dev/test only).  Refused at boot when
    # wrapper_env=prod (see validator below).
    rate_limit_bypass: bool = False
    # Per-IP RPM cap for unauthenticated / malformed-token requests.
    # Prevents argon2-burn DoS amplification (red-team C2 fix).
    ip_pre_auth_rpm: int = 30
    # Trust X-Forwarded-For header for client IP resolution.
    # Set True only when running behind Caddy/nginx in prod.
    trust_proxy: bool = False
    # In-process tier-limits cache TTL in seconds (default 5 min).
    tier_cache_ttl_seconds: int = 300

    # ── Auth ───────────────────────────────────────────────────────────────
    # Admin token protects /admin/* endpoints. Required in prod; defaults to
    # a placeholder in dev/test so unit tests don't raise on import.
    # Rotate periodically. Never log or expose this value.
    admin_token: SecretStr = SecretStr("dev-admin-token-replace-in-prod")

    # ── Audit log ──────────────────────────────────────────────────────────
    # When False (default): prompt stored as sha256 hash only — never raw text.
    # Set True ONLY in dev for debugging; NEVER in prod.
    audit_log_prompt: bool = False
    # Rows older than this many days deleted by daily retention cron.
    audit_log_retention_days: int = 90

    # ── Stderr archive ─────────────────────────────────────────────────────
    # Local directory for failed-job stderr archives (dev / no-S3 mode).
    stderr_archive_local_dir: str = "/var/codex-stderr"
    # Optional S3/B2 URL (e.g. s3://my-bucket/codex-stderr).
    # If set, archives are written to S3 instead of local disk.
    stderr_archive_s3_url: str | None = None
    # Retention in days for stderr archives (S3 lifecycle / local cron).
    stderr_retention_days: int = 14

    # ── Alert webhooks ─────────────────────────────────────────────────────
    # POST alerts here when Codex session goes unhealthy. None = disabled.
    webhook_alert_url: str | None = None
    # "slack" shapes payload as Slack blocks; "http" sends generic JSON.
    webhook_alert_kind: str = "http"

    # ── Input validation ───────────────────────────────────────────────────
    # Maximum total prompt character count (chat + responses + jobs).
    prompt_max_chars: int = 262_144  # 256k chars
    # Timeout (seconds) for repo URL HEAD-check before enqueue.
    repo_head_timeout: int = 5
    # Redis TTL (seconds) to cache a positive HEAD result.
    repo_head_cache_seconds: int = 300

    # ── Observability ──────────────────────────────────────────────────────
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str | None = None  # None → no-op tracer
    otel_service_name: str = "codex-wrapper-gateway"
    # Sampling ratio for OTEL traces (0.0–1.0). 0.1 = 10% of requests sampled.
    # Error spans are always recorded regardless of this ratio.
    otel_sampler_ratio: float = 0.1
    # Enable Prometheus metrics endpoint.
    metrics_enabled: bool = True
    # Internal path for Prometheus scrape — Caddy MUST NOT reverse-proxy this.
    internal_metrics_path: str = "/_internal/metrics"

    @model_validator(mode="after")
    def _prod_safety_checks(self) -> Settings:
        """Enforce prod-only safety invariants at startup."""
        if self.wrapper_env == "prod":
            token = self.admin_token.get_secret_value()
            if token == "dev-admin-token-replace-in-prod":
                raise ValueError("ADMIN_TOKEN must be set to a strong secret in prod environment")
            if self.rate_limit_bypass:
                raise ValueError("RATE_LIMIT_BYPASS must not be set to True in prod environment")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance.

    Raises ``ValidationError`` on first call if any required env var is absent.
    The cache ensures environment is read exactly once at startup.

    mypy cannot infer that pydantic-settings populates required fields from
    environment variables, so we suppress the false-positive call-arg error.
    """
    return Settings()  # type: ignore[call-arg]
