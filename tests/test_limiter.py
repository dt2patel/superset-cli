from __future__ import annotations

from datetime import datetime

import pytest

from tathya.config import SafetyConfig
from tathya.errors import LimitError
from tathya.limiter import Limiter, in_blackout


def test_rate_limit_hard_stops_a_retry_loop(tmp_path):
    limiter = Limiter(
        SafetyConfig(max_queries_per_minute=3, max_concurrent_queries=4, max_queries_per_day=100),
        tmp_path,
    )
    allowed = 0
    for _ in range(10):
        try:
            with limiter.acquire("db:x"):
                allowed += 1
        except LimitError:
            pass
    assert allowed == 3


def test_daily_cap(tmp_path):
    limiter = Limiter(
        SafetyConfig(max_queries_per_minute=100, max_concurrent_queries=4, max_queries_per_day=2),
        tmp_path,
    )
    allowed = 0
    for _ in range(5):
        try:
            with limiter.acquire("db:x"):
                allowed += 1
        except LimitError:
            pass
    assert allowed == 2


def test_concurrency_cap_within_one_process(tmp_path):
    """The watch runner evaluates several watches in one process."""
    limiter = Limiter(
        SafetyConfig(max_queries_per_minute=100, max_concurrent_queries=1, max_queries_per_day=100),
        tmp_path,
    )
    with limiter.acquire("db:x"):
        with pytest.raises(LimitError):
            with limiter.acquire("db:x"):
                pass


def test_slot_is_released_for_reuse(tmp_path):
    limiter = Limiter(
        SafetyConfig(max_queries_per_minute=100, max_concurrent_queries=1, max_queries_per_day=100),
        tmp_path,
    )
    with limiter.acquire("db:x"):
        pass
    with limiter.acquire("db:x"):
        pass


def test_blackout_window_blocks(tmp_path):
    limiter = Limiter(
        SafetyConfig(max_queries_per_minute=100, blackout_windows=("* 00:00-23:59",)), tmp_path
    )
    with pytest.raises(LimitError, match="blackout"):
        with limiter.acquire("db:x"):
            pass


@pytest.mark.parametrize(
    "spec,moment,expected",
    [
        ("Mon-Fri 09:00-11:00", datetime(2026, 8, 18, 10, 30), True),   # Tuesday
        ("Mon-Fri 09:00-11:00", datetime(2026, 8, 18, 12, 0), False),
        ("Mon-Fri 09:00-11:00", datetime(2026, 8, 22, 10, 0), False),   # Saturday
        ("* 22:00-06:00", datetime(2026, 8, 18, 23, 0), True),          # overnight
        ("* 22:00-06:00", datetime(2026, 8, 18, 3, 0), True),
        ("* 22:00-06:00", datetime(2026, 8, 18, 12, 0), False),
    ],
)
def test_blackout_parsing(spec, moment, expected):
    assert bool(in_blackout((spec,), moment)) is expected


def test_unparseable_window_is_an_error(tmp_path):
    with pytest.raises(LimitError):
        in_blackout(("whenever-ish",), datetime(2026, 8, 18, 10, 0))
