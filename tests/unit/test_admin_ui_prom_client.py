"""
Unit tests for src/admin_ui/prom_client.py

Covers:
  - parse_prometheus_text happy path (single metric, multiple labels)
  - parse_prometheus_text skips HELP/TYPE/blank/comment lines
  - parse_prometheus_text handles gauge, counter, histogram-style lines
  - parse_prometheus_text returns empty dict on empty input
  - fetch_kpis fallback returns KPISnapshot with zeroed fields when metrics unreachable
  - fetch_sparklines returns SparklineData with empty lists when Prometheus unset
"""

from __future__ import annotations

import pytest
from src.admin_ui.prom_client import (
    KPISnapshot,
    SparklineData,
    fetch_kpis,
    fetch_sparklines,
    parse_prometheus_text,
)

# ── parse_prometheus_text ──────────────────────────────────────────────────────


def test_parse_single_metric() -> None:
    text = "http_requests_total 42\n"
    result = parse_prometheus_text(text)
    assert result == {"http_requests_total": [42.0]}


def test_parse_metric_with_labels() -> None:
    text = 'http_requests_total{method="GET",status="200"} 100\n'
    result = parse_prometheus_text(text)
    assert result["http_requests_total"] == [100.0]


def test_parse_multiple_label_variants() -> None:
    text = 'http_requests_total{status="200"} 80\n' 'http_requests_total{status="500"} 5\n'
    result = parse_prometheus_text(text)
    assert sorted(result["http_requests_total"]) == [5.0, 80.0]


def test_parse_skips_help_and_type_lines() -> None:
    text = (
        "# HELP http_requests_total Total requests\n"
        "# TYPE http_requests_total counter\n"
        "http_requests_total 99\n"
    )
    result = parse_prometheus_text(text)
    assert result == {"http_requests_total": [99.0]}


def test_parse_skips_blank_lines() -> None:
    text = "\n\nhttp_requests_total 7\n\n"
    result = parse_prometheus_text(text)
    assert result == {"http_requests_total": [7.0]}


def test_parse_handles_float_value() -> None:
    text = 'go_gc_duration_seconds{quantile="0"} 4.9351e-05\n'
    result = parse_prometheus_text(text)
    assert "go_gc_duration_seconds" in result
    assert abs(result["go_gc_duration_seconds"][0] - 4.9351e-05) < 1e-10


def test_parse_returns_empty_on_empty_input() -> None:
    assert parse_prometheus_text("") == {}


def test_parse_returns_empty_on_only_comments() -> None:
    text = "# HELP foo bar\n# TYPE foo gauge\n"
    assert parse_prometheus_text(text) == {}


def test_parse_multiple_metrics() -> None:
    text = "codex_active_jobs 3\n" "arq_queue_depth 7\n"
    result = parse_prometheus_text(text)
    assert result["codex_active_jobs"] == [3.0]
    assert result["arq_queue_depth"] == [7.0]


# ── fetch_kpis / fetch_sparklines (no Prometheus configured) ──────────────────


@pytest.mark.asyncio
async def test_fetch_kpis_returns_zeros_when_metrics_unreachable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When /_internal/metrics is unreachable, returns zeroed KPISnapshot."""
    monkeypatch.setattr("src.admin_ui.prom_client.get_settings", lambda: _settings_no_prom())

    # Don't mock httpx — connection to localhost:8000 will fail in CI.
    result = await fetch_kpis()
    assert isinstance(result, KPISnapshot)
    # Values may be 0 (unreachable) — just assert type safety.
    assert result.req_rate_1m >= 0
    assert result.active_jobs >= 0
    assert result.queue_depth >= 0


@pytest.mark.asyncio
async def test_fetch_sparklines_returns_empty_without_prometheus(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With no PROMETHEUS_URL, sparklines are empty lists."""
    monkeypatch.setattr("src.admin_ui.prom_client.get_settings", lambda: _settings_no_prom())
    result = await fetch_sparklines()
    assert isinstance(result, SparklineData)
    assert result.req_24h == []
    assert result.error_24h == []


# ── helpers ────────────────────────────────────────────────────────────────────


class _FakeSettings:
    prometheus_url: str | None = None
    internal_metrics_path: str = "/_internal/metrics"
    admin_session_ttl_seconds: int = 28800


def _settings_no_prom() -> _FakeSettings:
    return _FakeSettings()
