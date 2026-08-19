"""Monitoring layer: aggregation, and the schedule-aware staleness rules.

Staleness gets the most attention here because the first implementation used a
flat age grace and produced 70 false positives against the live instance (a
monthly report idle for 17 days, a Friday-only alert idle for 4). A monitor that
cries wolf is worse than no monitor, so these rules are pinned to a fixed clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tathya import ops
from tathya.errors import ConfigError

# A Wednesday, 14:00 UTC.
NOW = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)


def report(**overrides) -> dict:
    base = {
        "id": 1, "name": "r", "type": "Report", "active": True,
        "last_state": "Success", "crontab": "0 9 * * *",
        "crontab_humanized": "daily 9am", "timezone": "UTC",
        "last_eval_dttm": (NOW - timedelta(hours=5)).replace(tzinfo=None).isoformat(),
    }
    base.update(overrides)
    return base


def iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat()


# ------------------------------------------------------------------- windows
@pytest.mark.parametrize("text,seconds",
                         [("30m", 1800), ("6h", 21600), ("2d", 172800), ("1w", 604800), (15, 900)])
def test_parse_window(text, seconds):
    assert ops.parse_window(text) == seconds


def test_parse_window_rejects_garbage():
    with pytest.raises(ConfigError):
        ops.parse_window("a while")


# --------------------------------------------------------------- aggregation
def rows():
    base = 1_800_000_000_000
    def q(db, status, seconds, user, n=1):
        return {"database": {"database_name": db}, "status": status, "rows": n,
                "start_time": base, "end_time": base + seconds * 1000,
                "user": {"id": 1, "first_name": user, "last_name": "X"},
                "sql": "SELECT 1", "executed_sql": "SELECT 1", "id": 1}
    return [
        q("slow-db", "success", 60, "Ana"),
        q("slow-db", "failed", 2, "Ana"),
        q("slow-db", "success", 1, "Bo"),
        q("fast-db", "success", 0.2, "Bo"),
    ]


def test_summarize_load_orders_worst_first():
    summary = ops.summarize_load(rows())
    assert summary["total_queries"] == 4
    assert summary["databases"][0]["database"] == "slow-db"
    slow = summary["databases"][0]
    assert slow["queries"] == 3
    assert slow["failures"] == 1
    assert slow["max_seconds"] == 60.0
    assert slow["distinct_users"] == 2
    assert summary["failure_rate_pct"] == 25.0


def test_timed_out_counts_as_a_failure():
    data = rows()
    data[0]["status"] = "timed_out"
    assert ops.summarize_load(data)["databases"][0]["failures"] == 2


def test_slowest_respects_threshold():
    slow = ops.slowest(rows(), min_seconds=10)
    assert [s["seconds"] for s in slow] == [60.0]


def test_failures_grouped_by_database_and_user():
    report_ = ops.failures(rows())
    assert report_["failed_total"] == 1
    assert report_["by_database"][0] == {"database": "slow-db", "failures": 1}


def test_queries_still_running_do_not_break_aggregation():
    data = rows()
    data[0]["end_time"] = None
    summary = ops.summarize_load(data)
    assert summary["total_queries"] == 4  # counted, just not timed


# ------------------------------------------------------- alert classification
def test_error_state_is_actionable():
    health = ops.classify_reports([report(last_state="Error")], now=NOW)
    assert health["counts"]["error"] == 1


def test_inactive_reports_are_not_flagged():
    health = ops.classify_reports([report(active=False, last_eval_dttm=iso(NOW - timedelta(days=90)))], now=NOW)
    assert health["counts"]["inactive"] == 1
    assert health["counts"]["stale"] == 0


def test_never_run_is_its_own_state():
    health = ops.classify_reports([report(last_eval_dttm=None)], now=NOW)
    assert health["counts"]["never_run"] == 1


def test_monthly_report_idle_for_days_is_not_stale():
    """The original bug: a flat grace flagged monthly reports as stale."""
    monthly = report(crontab="0 0 1 * *", crontab_humanized="monthly",
                     last_eval_dttm=iso(datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc)))
    health = ops.classify_reports([monthly], now=NOW)  # 18 days later
    assert health["counts"]["stale"] == 0, health["stale"]
    assert health["counts"]["healthy"] == 1


def test_weekly_report_idle_for_days_is_not_stale():
    """Friday-only alert, checked on a Wednesday: idle by design."""
    weekly = report(crontab="0 15 * * 5", crontab_humanized="Fridays",
                    last_eval_dttm=iso(datetime(2026, 8, 14, 15, 2, tzinfo=timezone.utc)))
    health = ops.classify_reports([weekly], now=NOW)
    assert health["counts"]["stale"] == 0, health["stale"]
    assert health["counts"]["healthy"] == 1


def test_daily_report_that_missed_its_run_is_stale():
    overdue = report(last_eval_dttm=iso(NOW - timedelta(days=3)))
    health = ops.classify_reports([overdue], now=NOW)
    assert health["counts"]["stale"] == 1
    entry = health["stale"][0]
    assert entry["overdue_by_hours"] == pytest.approx(5.0, abs=0.2)  # 09:00 today
    assert entry["expected_last_run"].startswith("2026-08-19T09:00")


def test_daily_report_that_just_ran_is_healthy():
    fresh = report(last_eval_dttm=iso(NOW - timedelta(minutes=10)))
    assert ops.classify_reports([fresh], now=NOW)["counts"]["healthy"] == 1


def test_grace_period_avoids_flagging_a_run_still_queued():
    """At 09:05 a 09:00 job may simply not have been picked up yet."""
    just_after = datetime(2026, 8, 19, 9, 5, tzinfo=timezone.utc)
    pending = report(last_eval_dttm=iso(datetime(2026, 8, 18, 9, 1, tzinfo=timezone.utc)))
    assert ops.classify_reports([pending], now=just_after)["counts"]["stale"] == 0


def test_unparseable_schedule_falls_back_to_flat_grace():
    broken = report(crontab="not a cron", last_eval_dttm=iso(NOW - timedelta(days=2)))
    health = ops.classify_reports([broken], now=NOW)
    assert health["unschedulable"] == 1
    assert health["counts"]["stale"] == 1
    assert "no parseable schedule" in health["stale"][0]["overdue_reason"]


def test_schedule_is_evaluated_in_the_reports_own_timezone():
    """A 09:00 America/New_York job is 13:00 UTC; at 14:00 UTC it is due today."""
    ny = report(timezone="America/New_York",
                last_eval_dttm=iso(NOW - timedelta(days=1)))
    health = ops.classify_reports([ny], now=NOW)
    assert health["counts"]["stale"] == 1
