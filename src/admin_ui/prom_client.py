"""
Prometheus query helper for admin dashboard.

Two modes:
  1. Remote Prometheus: query ``settings.prometheus_url`` via /api/v1/query
     and /api/v1/query_range when PROMETHEUS_URL is set.
  2. Local fallback: fetch ``/_internal/metrics`` text-format from localhost
     and parse it manually when PROMETHEUS_URL is unset.

Public interface
----------------
fetch_kpis(base_url) -> KPISnapshot
fetch_sparkline(base_url, metric, hours) -> list[float]

Both functions accept an optional ``base_url`` override (used in tests).
When base_url is None, they resolve from settings automatically.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.settings import get_settings

# ── Server-side KPI cache (5s TTL) ───────────────────────────────────────────
# Single-worker deployment — in-process dict is sufficient; no Redis needed.
# Falls back to last cached value with stale=True if Prometheus is down.

_kpi_cache: dict[str, Any] = {
    "snapshot": None,       # KPISnapshot | None
    "fetched_at": 0.0,      # epoch seconds of last successful fetch
    "stale": False,         # True when last fetch failed and we serve cached
}
_KPI_CACHE_TTL = 5.0  # seconds

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class KPISnapshot:
    req_rate_1m: float = 0.0      # requests/s (1-min rate)
    error_rate_5m: float = 0.0    # error fraction 0–1 (5-min rate)
    active_jobs: float = 0.0      # current in-flight jobs
    queue_depth: float = 0.0      # arq queue length


@dataclass
class SparklineData:
    req_24h: list[float] = field(default_factory=list)    # hourly req counts
    error_24h: list[float] = field(default_factory=list)  # hourly error counts


# ── Prometheus metric names ────────────────────────────────────────────────────

_PROM_HTTP_REQUESTS_TOTAL = "http_requests_total"
_PROM_ACTIVE_JOBS = "codex_active_jobs"
_PROM_QUEUE_DEPTH = "arq_queue_depth"

# Prometheus query expressions
_QUERY_REQ_RATE_1M = 'rate(http_requests_total[1m])'
_QUERY_ERROR_RATE_5M = (
    'rate(http_requests_total{status=~"5.."}[5m]) / '
    'rate(http_requests_total[5m])'
)
_QUERY_ACTIVE_JOBS = 'codex_active_jobs'
_QUERY_QUEUE_DEPTH = 'arq_queue_depth'


# ── Remote Prometheus helpers ─────────────────────────────────────────────────

async def _prom_instant(client: httpx.AsyncClient, base: str, query: str) -> float:
    """Query Prometheus instant value; return 0.0 on any error."""
    try:
        resp = await client.get(
            f"{base}/api/v1/query",
            params={"query": query},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except Exception:
        pass
    return 0.0


async def _prom_range(
    client: httpx.AsyncClient,
    base: str,
    query: str,
    hours: int = 24,
    step: int = 3600,
) -> list[float]:
    """Query Prometheus range; return empty list on any error."""
    end = int(time.time())
    start = end - hours * 3600
    try:
        resp = await client.get(
            f"{base}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return [float(v[1]) for v in results[0].get("values", [])]
    except Exception:
        pass
    return []


# ── Local text-format parser ───────────────────────────────────────────────────

_METRIC_RE = re.compile(
    r'^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\{[^}]*\})?\s+(?P<value>[0-9.eE+\-]+)'
)


def parse_prometheus_text(text: str) -> dict[str, list[float]]:
    """Parse Prometheus text-format exposition into {metric_name: [values]}.

    Only parses non-comment, non-blank lines with the standard metric format.
    Ignores HELP/TYPE lines. Returns all sample values per metric name
    (label-stripped) so callers can sum/average as needed.
    """
    result: dict[str, list[float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        try:
            val = float(m.group("value"))
        except ValueError:
            continue
        result.setdefault(name, []).append(val)
    return result


def _sum_metric(parsed: dict[str, list[float]], name: str) -> float:
    """Return sum of all label-variants for a metric name."""
    return sum(parsed.get(name, [0.0]))


async def _fetch_local_metrics(internal_url: str) -> dict[str, list[float]]:
    """Fetch and parse /_internal/metrics from the local gateway."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(internal_url, timeout=3.0)
            resp.raise_for_status()
            return parse_prometheus_text(resp.text)
    except Exception:
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_kpis(prometheus_base: str | None = None) -> KPISnapshot:
    """Return live KPI values from Prometheus or local metrics fallback."""
    settings = get_settings()
    base = prometheus_base or settings.prometheus_url

    if base:
        async with httpx.AsyncClient() as client:
            req_rate = await _prom_instant(client, base, _QUERY_REQ_RATE_1M)
            error_rate = await _prom_instant(client, base, _QUERY_ERROR_RATE_5M)
            active_jobs = await _prom_instant(client, base, _QUERY_ACTIVE_JOBS)
            queue_depth = await _prom_instant(client, base, _QUERY_QUEUE_DEPTH)
        return KPISnapshot(
            req_rate_1m=req_rate,
            error_rate_5m=error_rate,
            active_jobs=active_jobs,
            queue_depth=queue_depth,
        )

    # Fallback: parse /_internal/metrics from localhost.
    internal_url = f"http://localhost:8000{settings.internal_metrics_path}"
    parsed = await _fetch_local_metrics(internal_url)

    all_requests = _sum_metric(parsed, _PROM_HTTP_REQUESTS_TOTAL)
    active_jobs = _sum_metric(parsed, _PROM_ACTIVE_JOBS)
    queue_depth = _sum_metric(parsed, _PROM_QUEUE_DEPTH)

    return KPISnapshot(
        req_rate_1m=all_requests,   # raw counter — no rate calc without history
        error_rate_5m=0.0,
        active_jobs=active_jobs,
        queue_depth=queue_depth,
    )


async def fetch_sparklines(
    prometheus_base: str | None = None,
    hours: int = 24,
) -> SparklineData:
    """Return 24h sparkline data from Prometheus or empty lists as fallback."""
    settings = get_settings()
    base = prometheus_base or settings.prometheus_url

    if base:
        async with httpx.AsyncClient() as client:
            req_series = await _prom_range(
                client, base, f"rate({_PROM_HTTP_REQUESTS_TOTAL}[1h])", hours
            )
            err_series = await _prom_range(
                client,
                base,
                f'rate({_PROM_HTTP_REQUESTS_TOTAL}{{status=~"5.."}}[1h])',
                hours,
            )
        return SparklineData(req_24h=req_series, error_24h=err_series)

    # No Prometheus configured — return empty sparklines; dashboard shows
    # "no data" state gracefully.
    return SparklineData()


async def fetch_kpis_cached() -> tuple[KPISnapshot, bool]:
    """Return KPI snapshot with 5s server-side cache.

    Returns (snapshot, stale) where stale=True means the last live fetch
    failed and we are serving the previous cached value. If no cache exists
    yet and the fetch also fails, returns a zeroed KPISnapshot with stale=True.
    """
    now = time.monotonic()
    if _kpi_cache["snapshot"] is not None and (now - _kpi_cache["fetched_at"]) < _KPI_CACHE_TTL:
        # Cache hit — return current value (stale flag from last cycle)
        return _kpi_cache["snapshot"], _kpi_cache["stale"]

    # Cache miss or expired — attempt live fetch
    try:
        snapshot = await fetch_kpis()
        _kpi_cache["snapshot"] = snapshot
        _kpi_cache["fetched_at"] = now
        _kpi_cache["stale"] = False
        return snapshot, False
    except Exception:
        # Fetch failed — return last cached value if available, else zeros
        if _kpi_cache["snapshot"] is not None:
            _kpi_cache["stale"] = True
            return _kpi_cache["snapshot"], True
        # No prior cache — return zeros and mark stale
        fallback = KPISnapshot()
        _kpi_cache["snapshot"] = fallback
        _kpi_cache["fetched_at"] = now
        _kpi_cache["stale"] = True
        return fallback, True
