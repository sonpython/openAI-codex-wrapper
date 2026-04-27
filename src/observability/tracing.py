"""
OpenTelemetry tracing setup.

If OTEL_EXPORTER_OTLP_ENDPOINT is set, installs an OTLP gRPC exporter and
instruments FastAPI automatically.  Otherwise installs a no-op TracerProvider
so all `tracer.start_as_current_span(...)` calls are safe to call in every
environment without conditional guards.

Usage (called once from lifespan):
    from src.observability.tracing import configure_tracing
    configure_tracing(settings, app)
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from src.settings import Settings

logger = structlog.get_logger(__name__)


def configure_tracing(settings: Settings, app: FastAPI) -> None:
    """Install OTLP exporter or fall back to no-op tracer.

    Args:
        settings: Application settings (reads otel_* fields).
        app: FastAPI instance to instrument (only when OTLP endpoint set).
    """
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.wrapper_env,
        }
    )

    if settings.otel_exporter_otlp_endpoint:
        _install_otlp(settings, resource, app)
    else:
        _install_noop(resource)


def _install_otlp(settings: Settings, resource: Resource, app: FastAPI) -> None:
    """Wire up the OTLP gRPC exporter and FastAPI auto-instrumentation."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=True,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)
        logger.info(
            "otel_configured",
            endpoint=settings.otel_exporter_otlp_endpoint,
            mode="otlp",
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("otel_setup_failed", error=str(exc), fallback="noop")
        _install_noop(resource)


def _install_noop(resource: Resource) -> None:
    """Install a no-op tracer so span calls are safe but produce nothing."""
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)
    logger.info("otel_configured", mode="noop")
