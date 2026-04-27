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

    # ── Chat completions ───────────────────────────────────────────────────
    # Default timeout for a single chat completion request (sync or stream).
    chat_default_timeout_seconds: int = 120
    # Maximum assembled prompt length in characters — guards against runaway
    # prompts before they reach the subprocess. Checked in build_prompt().
    chat_max_prompt_chars: int = 200_000

    # ── Auth ───────────────────────────────────────────────────────────────
    # Admin token protects /admin/* endpoints. Required in prod; defaults to
    # a placeholder in dev/test so unit tests don't raise on import.
    # Rotate periodically. Never log or expose this value.
    admin_token: SecretStr = SecretStr("dev-admin-token-replace-in-prod")

    # ── Observability ──────────────────────────────────────────────────────
    log_level: str = "INFO"
    otel_exporter_otlp_endpoint: str | None = None  # None → no-op tracer
    otel_service_name: str = "codex-wrapper-gateway"

    @model_validator(mode="after")
    def _require_admin_token_in_prod(self) -> Settings:
        """Raise if running in prod with the default placeholder admin token."""
        if self.wrapper_env == "prod":
            token = self.admin_token.get_secret_value()
            if token == "dev-admin-token-replace-in-prod":
                raise ValueError("ADMIN_TOKEN must be set to a strong secret in prod environment")
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
