"""
Unit tests for src/gateway/rate_limit_reset_format.py.

Covers every format branch: sub-minute, minute-range, hour-range, and the
convenience format_reset_ms() wrapper.
"""

from __future__ import annotations

import pytest
from src.gateway.rate_limit_reset_format import format_reset, format_reset_ms


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0s"),
        (1, "1s"),
        (30, "30s"),
        (45, "45s"),
        (59, "59s"),
        # Ceiling rounding
        (0.1, "1s"),
        (0.9, "1s"),
        (58.1, "59s"),
        # Minute boundary
        (60, "1m00s"),
        (61, "1m01s"),
        (72, "1m12s"),
        (423, "7m03s"),
        (432, "7m12s"),
        (3599, "59m59s"),
        # Hour boundary
        (3600, "1h00m00s"),
        (3601, "1h00m01s"),
        (3660, "1h01m00s"),
        (3930, "1h05m30s"),
        (7200, "2h00m00s"),
    ],
)
def test_format_reset(seconds: float, expected: str) -> None:
    assert format_reset(seconds) == expected


def test_format_reset_negative_clamped_to_zero() -> None:
    """Negative input treated as 0."""
    assert format_reset(-5) == "0s"


def test_format_reset_ms_conversion() -> None:
    """format_reset_ms divides by 1000 and delegates to format_reset."""
    assert format_reset_ms(45_000) == "45s"
    assert format_reset_ms(60_000) == "1m00s"
    assert format_reset_ms(7_200_000) == "2h00m00s"


def test_format_reset_ms_sub_second_rounds_up() -> None:
    """100ms -> 1s (ceiling)."""
    assert format_reset_ms(100) == "1s"
