"""
Prometheus metrics registry stub.

Creates a dedicated CollectorRegistry so we don't pollute the default registry
with test-process metrics.  The ASGI app returned by ``make_metrics_app()``
is mounted at ``/metrics`` in the FastAPI factory.

Counters and histograms for business logic are added in phase 07.
"""

from __future__ import annotations

import os
from typing import Any

from prometheus_client import CollectorRegistry, make_asgi_app

# Module-level registry — import this in any module that defines metrics.
registry = CollectorRegistry(auto_describe=True)


def make_metrics_app() -> Any:  # noqa: ANN401 — prometheus_client not typed
    """Return an ASGI app that serves Prometheus metrics at /metrics.

    Supports multiprocess mode (PROMETHEUS_MULTIPROC_DIR env var set) so
    metrics aggregate correctly across uvicorn worker processes.
    """
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:  # pragma: no cover
        import prometheus_client.multiprocess as _mp  # noqa: PLC0415

        _mp.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]

    return make_asgi_app(registry=registry)
