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


# ------------------------------------------------------------------ windowed watches
from tathya.watch import compute_window, render_window_sql, write_handoff  # noqa: E402


def windowed(**window) -> Watch:
    return make_watch(
        source={"type": "sql", "database": "x",
                "sql": "SELECT 1 WHERE t >= '{{window_start_utc}}' AND t < '{{window_end_utc}}'",
                "window": window or {"lag": "20m", "initial": "30m", "max": "24h"}},
        notify={"mode": "each_alert"},
    )


def test_first_window_looks_back_initial_and_trails_by_lag():
    window = compute_window(windowed(), {}, now=10_000.0)
    assert window["end"] == 10_000.0 - 1200 and window["start"] == window["end"] - 1800
    assert not window["empty"] and window["truncated_from_utc"] is None


def test_next_window_starts_where_the_last_one_ended():
    window = compute_window(windowed(), {"window_end": 7_000.0}, now=10_000.0)
    assert window["start"] == 7_000.0 and window["end"] == 8_800.0


def test_long_gap_is_capped_and_reported_as_truncated():
    watch = windowed(lag="0s", initial="30m", max="1h")
    window = compute_window(watch, {"window_end": 0.0}, now=10_000.0)
    assert window["start"] == 10_000.0 - 3600
    assert window["truncated_from_utc"] == "1970-01-01 00:00:00"


def test_run_sooner_than_lag_gives_an_empty_window():
    assert compute_window(windowed(), {"window_end": 9_000.0}, now=10_000.0)["empty"] is True


def test_window_tokens_render_as_utc_literals():
    window = compute_window(windowed(lag="0s"), {"window_end": 0.0}, now=60.0)
    sql = render_window_sql("a '{{window_start_utc}}' b '{{window_end_utc}}'", window)
    assert sql == "a '1970-01-01 00:00:00' b '1970-01-01 00:01:00'"


def test_each_alert_mode_reports_every_firing_run():
    notify, reason = should_notify(windowed(), Evaluation(STATUS_ALERT, ""), {"status": "alert"})
    assert notify is True and reason == "each_alert"


def test_each_alert_mode_stays_quiet_while_healthy():
    assert should_notify(windowed(), Evaluation(STATUS_OK, ""), {"status": "ok"})[0] is False


def _write_watch(folder: Path, body: str, name: str = "w.yaml") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body)
    return path


def test_windowed_sql_must_use_both_tokens(tmp_path):
    _write_watch(tmp_path, """
name: w
source: {type: sql, database: x, sql: "SELECT 1 WHERE t >= '{{window_start_utc}}'", window: {lag: 20m}}
condition: {type: rowcount, op: ">", value: 0}
""")
    with pytest.raises(ConfigError):
        load_all(tmp_path)


def test_sql_file_resolves_beside_the_watch(tmp_path):
    (tmp_path / "q.sql").write_text("SELECT 1 WHERE t >= '{{window_start_utc}}' AND t < '{{window_end_utc}}'")
    _write_watch(tmp_path, """
name: w
source: {type: sql, database: x, sql_file: q.sql, window: {lag: 20m}}
condition: {type: rowcount, op: ">", value: 0}
notify: {mode: each_alert}
""")
    watch = load_all(tmp_path)[0]
    assert "{{window_end_utc}}" in watch.sql_text and watch.window == {"lag": "20m"}


def test_missing_sql_file_is_a_config_error(tmp_path):
    _write_watch(tmp_path, """
name: w
source: {type: sql, database: x, sql_file: nope.sql}
condition: {type: rowcount, op: ">", value: 0}
""")
    with pytest.raises(ConfigError):
        load_all(tmp_path)


def test_unknown_notify_mode_is_rejected(tmp_path):
    _write_watch(tmp_path, """
name: w
source: {type: sql, database: x, sql: SELECT 1}
condition: {type: rowcount, op: ">", value: 0}
notify: {mode: sometimes}
""")
    with pytest.raises(ConfigError):
        load_all(tmp_path)


def test_handoff_file_carries_rows_and_appears_whole(tmp_path):
    import json

    inbox = tmp_path / "inbox"
    watch = make_watch(notify={"handoff_dir": str(inbox)})
    evaluation = Evaluation(STATUS_ALERT, "2 row(s)", rows=2,
                            result_rows=[{"issue": "MISSING"}, {"issue": "ERROR"}])
    path = Path(write_handoff(watch, {"watch": "w", "status": "alert"}, evaluation))
    assert path.parent == inbox and path.name.endswith("-w.json")
    payload = json.loads(path.read_text())
    assert payload["rows_total"] == 2 and payload["result_rows"][1]["issue"] == "ERROR"
    assert [p.name for p in inbox.iterdir()] == [path.name]  # no temp file left in the watched folder


def test_limit_error_defers_without_touching_status_or_window(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import tathya.watch as watch_mod
    from tathya.errors import LimitError

    def busy(*args, **kwargs):
        raise LimitError("All 1 concurrent query slot(s) are in use.")

    monkeypatch.setattr(watch_mod, "evaluate", busy)
    recorded = []
    ctx = SimpleNamespace(audit=SimpleNamespace(record=lambda **kw: recorded.append(kw)))
    state = WatchState(tmp_path / "state.json")
    state.update("w", status="alert", window_end=1_000.0)
    result = watch_mod.run_watch(ctx, windowed(), state=state)
    assert result["status"] == "deferred" and result["notified"] is False
    after = state.get("w")
    assert after["status"] == "alert" and after["window_end"] == 1_000.0
    assert "concurrent" in after["last_deferred_reason"] and recorded[0]["outcome"] == "deferred"


def test_each_alert_mode_does_not_announce_a_clean_window_after_an_alert():
    notify, reason = should_notify(windowed(), Evaluation(STATUS_OK, ""), {"status": "alert"})
    assert notify is False and reason == "window_clean"


def test_each_alert_mode_still_reports_errors():
    assert should_notify(windowed(), Evaluation(STATUS_ERROR, ""), {"status": "ok"})[0] is True
