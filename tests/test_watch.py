from __future__ import annotations

from pathlib import Path

import pytest

from tathya.errors import ConfigError, UsageError
from tathya.watch import (
    STATUS_ALERT,
    STATUS_ERROR,
    STATUS_OK,
    Evaluation,
    Watch,
    WatchState,
    is_due,
    load_all,
    parse_duration,
    should_notify,
)


def make_watch(**overrides) -> Watch:
    base = dict(
        name="w",
        path=Path("/tmp/w.yaml"),
        source={"type": "sql", "database": "x", "sql": "SELECT 1"},
        condition={"type": "rowcount", "op": ">", "value": 0},
    )
    base.update(overrides)
    return Watch(**base)


@pytest.mark.parametrize("text,seconds", [("30m", 1800), ("2h", 7200), ("90s", 90), ("1d", 86400), (300, 300)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_duration("soonish")


def test_healthy_first_observation_stays_quiet():
    notify, reason = should_notify(make_watch(), Evaluation(STATUS_OK, ""), {})
    assert notify is False and reason == "first_observation"


def test_problem_on_first_observation_reports():
    assert should_notify(make_watch(), Evaluation(STATUS_ALERT, ""), {})[0] is True


def test_persistent_alert_does_not_repeat():
    """The whole point of transition-based reporting: no spam."""
    notify, reason = should_notify(make_watch(), Evaluation(STATUS_ALERT, ""), {"status": "alert"})
    assert notify is False and reason == "unchanged"


def test_recovery_reports():
    notify, reason = should_notify(make_watch(), Evaluation(STATUS_OK, ""), {"status": "alert"})
    assert notify is True and reason == "status_change"


def test_error_transition_reports():
    assert should_notify(make_watch(), Evaluation(STATUS_ERROR, ""), {"status": "ok"})[0] is True


def test_renotify_after_interval():
    import time

    watch = make_watch(notify={"renotify_after": "1h"})
    stale = {"status": "alert", "last_notified_at": time.time() - 7200}
    notify, reason = should_notify(watch, Evaluation(STATUS_ALERT, ""), stale)
    assert notify is True and reason == "renotify_interval"


def test_renotify_not_yet_due():
    import time

    watch = make_watch(notify={"renotify_after": "1h"})
    recent = {"status": "alert", "last_notified_at": time.time() - 60}
    assert should_notify(watch, Evaluation(STATUS_ALERT, ""), recent)[0] is False


def test_renotify_when_never_notified():
    """An alerting watch with no record of a notification should still report."""
    watch = make_watch(notify={"renotify_after": "1h"})
    assert should_notify(watch, Evaluation(STATUS_ALERT, ""), {"status": "alert"})[0] is True


def test_due_calculation():
    watch = make_watch(schedule={"every": "30m"})
    assert is_due(watch, {}) is True
    assert is_due(watch, {"last_run_at": 1000.0}, now=1000.0 + 60) is False
    assert is_due(watch, {"last_run_at": 1000.0}, now=1000.0 + 1801) is True


def test_state_round_trip(tmp_path):
    state = WatchState(tmp_path / "state.json")
    state.update("w", status="alert", value=5)
    assert state.get("w")["status"] == "alert"
    state.update("w", status="ok")
    assert state.get("w")["status"] == "ok"
    assert state.get("w")["value"] == 5  # unrelated fields survive


def test_missing_condition_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "name: bad\nsource:\n  type: sql\n  database: x\n  sql: SELECT 1\n"
    )
    with pytest.raises(ConfigError, match="condition"):
        load_all(tmp_path)


def test_incomplete_sql_source_is_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("name: bad\nsource:\n  type: sql\n")
    with pytest.raises(ConfigError, match="database"):
        load_all(tmp_path)


def test_unknown_source_type_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "name: bad\nsource:\n  type: telepathy\ncondition:\n  type: rowcount\n  op: '>'\n  value: 0\n"
    )
    with pytest.raises(ConfigError, match="source type"):
        load_all(tmp_path)
