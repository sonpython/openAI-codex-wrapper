"""
Prometheus metrics registry and named instruments.

Single module-level ``CollectorRegistry`` — import this in every module that
needs to record a metric.  Use the module-level instances directly:

    from src.observability.metrics import HTTP_REQUESTS, HTTP_DURATION
    HTTP_REQUESTS.labels(route="/v1/chat", status="200", method="POST").inc()
    HTTP_DURATION.labels(route="/v1/chat").observe(0.42)

Instruments are defined once at import time; reuse across worker processes
requires PROMETHEUS_MULTIPROC_DIR (see make_metrics_app).

``make_metrics_app()`` returns an ASGI app mounted at ``/_internal/metrics``
in the gateway.  Phase 10 Caddyfile MUST block this path from public access.
"""

from __future__ import annotations

import os
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,
)

# ── Shared registry ────────────────────────────────────────────────────────────
# Single registry used by all instruments.  Tests can import this to call
# generate_latest() directly without spinning up the ASGI app.
registry = CollectorRegistry(auto_describe=True)

# ── HTTP ───────────────────────────────────────────────────────────────────────
HTTP_REQUESTS: Counter = Counter(
    "http_requests_total",
    "Total HTTP requests",
    labelnames=["route", "status", "method"],
    registry=registry,
)

HTTP_DURATION: Histogram = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=["route"],
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 30],
    registry=registry,
)

# ── Codex subprocess ───────────────────────────────────────────────────────────
CODEX_SUBPROCESS_DURATION: Histogram = Histogram(
    "codex_subprocess_duration_seconds",
    "Duration of codex subprocess execution",
    labelnames=["exit_code_class"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300],
    registry=registry,
)

CODEX_SUBPROCESS_EXIT_CODE: Counter = Counter(
    "codex_subprocess_exit_code_total",
    "Codex subprocess exit codes",
    labelnames=["code"],
    registry=registry,
)

CODEX_EVENT_TOTAL: Counter = Counter(
    "codex_event_total",
    "Codex events parsed from stdout stream",
    labelnames=["type"],
    registry=registry,
)

CODEX_ACTIVE_SUBPROCESS: Gauge = Gauge(
    "codex_active_subprocess",
    "Number of currently running codex subprocesses",
    registry=registry,
)

# ── Arq / queue ────────────────────────────────────────────────────────────────
ARQ_QUEUE_DEPTH: Gauge = Gauge(
    "arq_queue_depth",
    "Number of jobs waiting in the Arq queue",
    registry=registry,
)

ARQ_JOBS_ACTIVE: Gauge = Gauge(
    "arq_jobs_active",
    "Number of Arq jobs currently executing",
    registry=registry,
)

ARQ_JOB_DURATION: Histogram = Histogram(
    "arq_job_duration_seconds",
    "Duration of Arq job execution",
    labelnames=["outcome"],
    buckets=[1, 5, 15, 30, 60, 120, 300, 600, 900],
    registry=registry,
)

ARQ_JOBS_TOTAL: Counter = Counter(
    "arq_jobs_total",
    "Total Arq jobs by final status",
    labelnames=["status"],
    registry=registry,
)

# ── Rate limiting ──────────────────────────────────────────────────────────────
RATE_LIMIT_REJECTIONS: Counter = Counter(
    "rate_limit_rejections_total",
    "Rate limit rejections by dimension",
    labelnames=["dimension"],
    registry=registry,
)

RATE_LIMIT_REMAINING: Gauge = Gauge(
    "rate_limit_remaining",
    "Remaining quota by dimension and tier",
    labelnames=["dimension", "tier"],
    registry=registry,
)

# ── Auth ───────────────────────────────────────────────────────────────────────
AUTH_REJECTIONS: Counter = Counter(
    "auth_rejections_total",
    "Authentication rejections by reason",
    labelnames=["reason"],
    registry=registry,
)

# ── Database ───────────────────────────────────────────────────────────────────
DB_QUERY_DURATION: Histogram = Histogram(
    "db_query_duration_seconds",
    "Database query duration by operation",
    labelnames=["op"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5],
    registry=registry,
)

DB_POOL_ACTIVE: Gauge = Gauge(
    "db_pool_active",
    "Active database pool connections",
    labelnames=["pool"],
    registry=registry,
)

DB_POOL_IDLE: Gauge = Gauge(
    "db_pool_idle",
    "Idle database pool connections",
    labelnames=["pool"],
    registry=registry,
)


def make_metrics_app() -> Any:  # noqa: ANN401 — prometheus_client not typed
    """Return an ASGI app serving Prometheus metrics.

    Mount at ``/_internal/metrics`` (NOT ``/metrics``) so Caddy can block it.
    Supports multiprocess mode via PROMETHEUS_MULTIPROC_DIR env var.
    """
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:  # pragma: no cover
        import prometheus_client.multiprocess as _mp  # noqa: PLC0415

        _mp.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]

    return make_asgi_app(registry=registry)
