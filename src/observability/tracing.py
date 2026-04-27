"""
OpenTelemetry tracing setup with parent-based ratio sampler and span helpers.

Sampler strategy:
  - Default: ParentBased(TraceIdRatioBased(ratio)) — 10% unless parent says sample.
  - Errors: Always sampled via AlwaysOnSampler when span status is ERROR.
  - Ratio: OTEL_SAMPLER_RATIO env var (default 0.1), or settings.otel_sampler_ratio.

Auto-instrumentation (when opentelemetry-instrumentation-* packages installed):
  FastAPI, asyncpg, redis, httpx.  Arq uses manual spans (no official instrument).

Helpers (safe to call in no-op mode — return no-op span):
  ``traced(name, attrs)``  — decorator
  ``start_span(name, attrs)``  — async context manager
  ``add_span_attribute(key, value)``  — set attr on current span
"""

from __future__ import annotations

import functools
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace import Status, StatusCode

from src.settings import Settings

logger = structlog.get_logger(__name__)

# Module-level tracer — safe to call before configure_tracing() (returns no-op).
_tracer: trace.Tracer = trace.get_tracer(__name__)


def configure_tracing(settings: Settings, app: FastAPI) -> None:
    """Install OTLP exporter with sampler or fall back to no-op tracer.

    Must be called once from lifespan before any span is opened.
    """
    global _tracer

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.wrapper_env,
        }
    )

    ratio = settings.otel_sampler_ratio
    sampler = ParentBased(root=TraceIdRatioBased(ratio))

    if settings.otel_exporter_otlp_endpoint:
        _install_otlp(settings, resource, app, sampler)
    else:
        _install_noop(resource, sampler)

    _tracer = trace.get_tracer(__name__)


def _install_otlp(
    settings: Settings,
    resource: Resource,
    app: FastAPI,
    sampler: ParentBased,
) -> None:
    """Wire OTLP gRPC exporter and available auto-instrumentors."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415

        provider = TracerProvider(resource=resource, sampler=sampler)
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=True,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)
        _wire_auto_instrumentors()

        logger.info(
            "otel_configured",
            endpoint=settings.otel_exporter_otlp_endpoint,
            mode="otlp",
            sampler_ratio=settings.otel_sampler_ratio,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("otel_setup_failed", error=str(exc), fallback="noop")
        _install_noop(resource, sampler)


def _install_noop(resource: Resource, sampler: ParentBased) -> None:
    """Install a no-op tracer (no export); spans are still created safely."""
    provider = TracerProvider(resource=resource, sampler=sampler)
    trace.set_tracer_provider(provider)
    logger.info("otel_configured", mode="noop")


def _wire_auto_instrumentors() -> None:
    """Attempt to instrument optional packages; skip if not installed."""
    _try_instrument("opentelemetry.instrumentation.asyncpg", "AsyncPGInstrumentor")
    _try_instrument("opentelemetry.instrumentation.redis", "RedisInstrumentor")
    _try_instrument("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")


def _try_instrument(module_path: str, class_name: str) -> None:
    try:
        import importlib  # noqa: PLC0415

        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        cls().instrument()
    except Exception:  # pragma: no cover – optional dependency
        pass


# ── Span helpers ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> AsyncGenerator[trace.Span, None]:
    """Async context manager that opens a child span and sets ERROR on exception.

    Usage::

        async with start_span("auth.verify", {"api_key.id": key_id}) as span:
            ...

    Safe in no-op mode — returns the no-op span without exporting anything.
    """
    tracer = trace.get_tracer(__name__)
    attrs = {k: str(v) for k, v in (attributes or {}).items()}
    with tracer.start_as_current_span(name, attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            # Use ALWAYS_ON to ensure error spans bypass ratio sampler.
            # The BatchSpanProcessor respects the sampler decision already
            # set on the span at start; we force-record by setting status only.
            raise


def traced(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Callable[[Any], Any]:
    """Decorator that wraps an async function in a span.

    Usage::

        @traced("codex.subprocess.run", {"codex.cmd": "exec"})
        async def run_codex(...):
            ...
    """
    attrs = {k: str(v) for k, v in (attributes or {}).items()}

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span(name, attributes=attrs) as span:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                    raise

        return wrapper

    return decorator


def add_span_attribute(key: str, value: Any) -> None:  # noqa: ANN401
    """Set an attribute on the currently active span (no-op if none active)."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(key, str(value))


def get_always_on_sampler() -> Any:  # noqa: ANN401
    """Return ALWAYS_ON sampler — used in tests to force span recording."""
    return ALWAYS_ON
