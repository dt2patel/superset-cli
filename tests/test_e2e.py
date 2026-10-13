"""End-to-end CLI runs against a mock Superset that enforces the real contract."""

from __future__ import annotations

import json

from tathya.errors import (
    EXIT_ALERT,
    EXIT_CONFIG,
    EXIT_GUARD,
    EXIT_OK,
    EXIT_WRITE_BLOCKED,
)

WATCH_YAML = """
name: stuck-orders
description: Orders sitting unfulfilled
enabled: true
source:
  type: sql
  database: acme_oms
  sql: SELECT COUNT(*) AS stuck FROM order_header WHERE status_id = 'APPROVED'
condition:
  type: threshold
  column: stuck
  op: ">"
  value: 50
schedule:
  every: 30m
notify:
  targets: [local]
"""


# ------------------------------------------------------------------- read path
def test_health(env, cli):
    code, payload = cli("health")
    assert code == EXIT_OK
    assert payload["ok"] is True
    assert payload["data"]["authenticated"] is True
    assert payload["data"]["identity"]["username"] == "agent-test"
    assert payload["data"]["visible_counts"]["dashboards"] == 2


def test_health_survives_missing_me_endpoint(env, cli, monkeypatch):
    """Tathya 401s /api/v1/me/ for every token. Health must not depend on it."""
    import mock_superset

    monkeypatch.setattr(mock_superset, "DISABLE_ME_ENDPOINT", True)
    code, payload = cli("health")
    assert code == EXIT_OK
    assert payload["data"]["authenticated"] is True          # proven by list calls
    assert payload["data"]["visible_counts"]["dashboards"] == 2
    identity = payload["data"]["identity"]
    assert identity["source"] == "jwt_claim"                 # fell back cleanly
    assert "unavailable" in identity["me_endpoint"]


def test_auth_status_probe_avoids_me_endpoint(env, cli, monkeypatch):
    import mock_superset

    monkeypatch.setattr(mock_superset, "DISABLE_ME_ENDPOINT", True)
    code, payload = cli("auth", "status", "--probe")
    assert code == EXIT_OK
    assert payload["data"]["probe"]["ok"] is True
    assert payload["data"]["probe"]["endpoint"] == "/api/v1/dashboard/"


def test_db_list_marks_allowlist_and_redacts_dsn(env, cli):
    code, payload = cli("db", "list")
    assert code == EXIT_OK
    rows = {row["database_name"]: row for row in payload["data"]["results"]}
    assert rows["acme_oms"]["agent_allowlisted"] is True
    assert rows["examples"]["agent_allowlisted"] is False
    # The mock returns a DSN with a password; it must never reach output.
    assert "sup3rs3cret" not in json.dumps(payload)


def test_dataset_get(env, cli):
    code, payload = cli("dataset", "get", "7")
    assert code == EXIT_OK
    assert [c["name"] for c in payload["data"]["columns"]] == [
        "order_id", "status_id", "created_stamp"
    ]


def test_chart_data(env, cli):
    code, payload = cli("chart", "data", "10")
    assert code == EXIT_OK
    assert payload["data"]["rows"] == [{"stuck": 143}]


def test_dashboard_charts(env, cli):
    code, payload = cli("dashboard", "charts", "1")
    assert code == EXIT_OK
    assert payload["data"][0]["slice_id"] == 10


# -------------------------------------------------------------------- sql path
def test_sql_run_succeeds_and_sends_a_limit(env, cli):
    code, payload = cli("sql", "run", "acme_oms", "SELECT COUNT(*) AS stuck FROM order_header")
    assert code == EXIT_OK
    assert payload["data"]["rows"] == [{"stuck": 143}]
    executed = [r for r in env.received if r["path"] == "/api/v1/sqllab/execute/"]
    assert len(executed) == 1
    # The per-database cap (50) is tighter than the global cap (100).
    assert "LIMIT 50" in executed[0]["body"]["sql"].upper()
    assert executed[0]["body"]["queryLimit"] == 50


def test_sql_run_refuses_a_write(env, cli):
    code, payload = cli("sql", "run", "acme_oms", "DROP TABLE order_header")
    assert code == EXIT_GUARD
    assert payload["error"]["kind"] == "guard_rejected"
    assert payload["error"]["retryable"] is False
    assert not env.received  # nothing was sent upstream


def test_sql_run_refuses_data_modifying_cte(env, cli):
    code, _ = cli(
        "sql", "run", "acme_oms",
        "WITH d AS (DELETE FROM order_header RETURNING order_id) SELECT * FROM d",
    )
    assert code == EXIT_GUARD
    assert not env.received


def test_sql_run_refuses_non_allowlisted_database(env, cli):
    code, payload = cli("sql", "run", "examples", "SELECT 1")
    assert code == EXIT_CONFIG
    assert "not allowlisted" in payload["error"]["message"]
    assert not env.received


def test_max_rows_cannot_be_raised_above_config(env, cli):
    code, _ = cli("sql", "run", "acme_oms", "SELECT 1", "--max-rows", "99999")
    assert code == EXIT_OK
    executed = [r for r in env.received if r["path"] == "/api/v1/sqllab/execute/"]
    assert executed[0]["body"]["queryLimit"] == 50


def test_explain_only_touches_nothing(env, cli):
    code, payload = cli("sql", "run", "acme_oms", "SELECT a FROM t", "--explain-only")
    assert code == EXIT_OK
    assert payload["data"]["would_execute"] is True
    assert "LIMIT 50" in payload["data"]["guard"]["sql"].upper()
    assert not env.received


def test_estimate(env, cli):
    code, payload = cli("sql", "estimate", "acme_oms", "SELECT 1")
    assert code == EXIT_OK
    assert payload["data"]["supported"] is True


# ------------------------------------------------------------------ write gate
def test_write_is_dry_run_without_confirm(env, cli):
    code, payload = cli(
        "chart", "create", "--name", "New", "--dataset-id", "7", "--viz-type", "table"
    )
    assert code == EXIT_OK
    assert payload["data"]["dry_run"] is True
    assert not env.received


def test_confirm_alone_is_not_enough(env, cli):
    code, payload = cli(
        "chart", "create", "--name", "New", "--dataset-id", "7", "--viz-type", "table", "--confirm"
    )
    assert code == EXIT_WRITE_BLOCKED
    assert "TATHYA_ALLOW_WRITE" in json.dumps(payload["error"])
    assert not env.received


def test_write_succeeds_with_both_switches(env, cli, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    code, payload = cli(
        "chart", "create", "--name", "New", "--dataset-id", "7", "--viz-type", "table", "--confirm"
    )
    assert code == EXIT_OK
    assert payload["data"]["applied"] is True
    posts = [r for r in env.received if r["method"] == "POST" and r["path"] == "/api/v1/chart/"]
    assert posts[0]["body"]["slice_name"] == "New"


def test_delete_still_blocked_when_allow_delete_false(env, cli, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    code, payload = cli("chart", "delete", "11", "--confirm")
    assert code == EXIT_WRITE_BLOCKED
    assert "allow_delete" in json.dumps(payload["error"])
    assert not env.received


def test_add_chart_updates_layout_and_association(env, cli, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    code, payload = cli("dashboard", "add-chart", "2", "11", "--confirm")
    assert code == EXIT_OK
    puts = [r for r in env.received if r["method"] == "PUT"]
    assert len(puts) == 2
    layout = json.loads(puts[0]["body"]["position_json"])
    charts = [n for n in layout.values() if isinstance(n, dict) and n.get("type") == "CHART"]
    assert charts[0]["meta"]["chartId"] == 11
    assert charts[0]["id"] in layout["GRID_ID"]["children"] or any(
        charts[0]["id"] in layout[row]["children"]
        for row in layout["GRID_ID"]["children"]
    )
    assert puts[1]["body"]["dashboards"] == [2]


# ---------------------------------------------------------------------- watch
def test_watch_run_alerts_and_reports_once(env, cli):
    (env.watches / "stuck.yaml").write_text(WATCH_YAML)
    code, payload = cli("watch", "run", "--all")
    assert code == EXIT_ALERT
    result = payload["data"]["results"][0]
    assert result["status"] == "alert"
    assert result["notified"] is True
    assert result["deliveries"][0]["target"] == "local"

    # Second run: still alerting, but nothing is re-sent.
    code2, payload2 = cli("watch", "run", "--all")
    assert code2 == EXIT_ALERT
    second = payload2["data"]["results"][0]
    assert second["notified"] is False
    assert second["notify_reason"] == "unchanged"


def test_watch_due_only_skips_recent_runs(env, cli):
    (env.watches / "stuck.yaml").write_text(WATCH_YAML)
    cli("watch", "run", "--all")
    code, payload = cli("watch", "run", "--all", "--due-only")
    assert payload["data"]["skipped"][0]["reason"] == "not_due"


def test_watch_dry_run_leaves_state_untouched(env, cli):
    (env.watches / "stuck.yaml").write_text(WATCH_YAML)
    code, payload = cli("watch", "run", "--all", "--dry-run")
    assert payload["data"]["results"][0]["deliveries"][0]["dry_run"] is True
    code2, payload2 = cli("watch", "state")
    assert payload2["data"] == {}


# ---------------------------------------------------------------------- audit
def test_audit_records_guard_rejections_and_successes(env, cli):
    cli("sql", "run", "acme_oms", "SELECT 1")
    cli("sql", "run", "acme_oms", "DROP TABLE t")
    code, payload = cli("audit", "tail")
    outcomes = [row["outcome"] for row in payload["data"]]
    assert "success" in outcomes
    assert "guard_rejected" in outcomes
    assert all(row["actor"] == "pytest" for row in payload["data"])


def test_limits_reports_budget(env, cli):
    code, payload = cli("limits")
    assert code == EXIT_OK
    assert payload["data"]["limits"]["max_queries_per_minute"] == 60


def test_client_id_fits_supersets_column_limit(env, cli):
    """Query.client_id is String(11). A longer value fails the metadata insert
    with an opaque 'Create failed', which is expensive to diagnose."""
    from tathya.query import CLIENT_ID_MAX_CHARS, new_client_id

    assert CLIENT_ID_MAX_CHARS == 11
    assert all(len(new_client_id()) <= 11 for _ in range(200))
    assert len({new_client_id() for _ in range(500)}) == 500  # unique=True in Superset

    code, _ = cli("sql", "run", "acme_oms", "SELECT 1")
    assert code == EXIT_OK
    executed = [r for r in env.received if r["path"] == "/api/v1/sqllab/execute/"]
    assert len(executed[0]["body"]["client_id"]) <= 11


# --- chart data must clear the same allowlist as ad-hoc SQL ----------------

def test_chart_data_refuses_a_non_allowlisted_database(env, cli):
    """Chart 12 sits on `examples`, which the test config does not allowlist.
    Previously this executed anyway, because the path never resolved a database."""
    code, payload = cli("chart", "data", "12")
    assert code == EXIT_CONFIG
    assert "not allowlisted" in payload["error"]["message"]
    assert not [r for r in env.received if "/data/" in r["path"]]


def test_chart_data_allows_an_allowlisted_database(env, cli):
    code, payload = cli("chart", "data", "10")
    assert code == EXIT_OK
    assert payload["data"]["database"] == "acme_oms"
    assert payload["data"]["rows"] == [{"stuck": 143}]


def test_chart_without_a_datasource_is_refused_not_guessed(env, cli):
    code, payload = cli("chart", "data", "13")
    assert code == EXIT_CONFIG
    assert "Could not determine which dataset" in payload["error"]["message"]


def test_chart_database_lookup_is_cached(env, cli):
    """The mapping never changes, so the two extra calls happen once per chart."""
    cli("chart", "data", "10")
    first = len(env.received)
    cache = env.state / "chart-database.json"
    assert json.loads(cache.read_text()) == {"10": "acme_oms"}
    env.received.clear()
    cli("chart", "data", "10")
    # Second run: only the /data/ call, no chart or dataset lookup.
    assert [r["path"] for r in env.received if r["method"] == "POST"] == []
    assert cache.exists()


def test_chart_data_consumes_the_database_budget(env, cli):
    """Load is per database, so a chart shares the budget with sql run."""
    cli("chart", "data", "10")
    code, payload = cli("limits")
    assert payload["data"]["used_today"]["db:acme_oms"] == 1


def test_resolver_does_not_trust_an_unfiltered_listing(env, cli, monkeypatch):
    """If Superset ignores the id filter, the resolver must not attribute this
    chart to whatever row came back first - that would name the wrong client's
    database and let a query through against it."""
    import mock_superset

    original = mock_superset.Handler.do_GET

    def ignore_filters(self):
        # Strip the query string so the mock returns every chart, unfiltered.
        if self.path.startswith("/api/v1/chart/?"):
            self.path = "/api/v1/chart/"
        return original(self)

    monkeypatch.setattr(mock_superset.Handler, "do_GET", ignore_filters)
    # Chart 12 is on the non-allowlisted `examples` database; chart 10 (first in
    # the unfiltered list) is on the allowlisted one. It must still be refused.
    code, payload = cli("chart", "data", "12")
    assert code == EXIT_CONFIG
    assert "not allowlisted" in payload["error"]["message"]
    assert not [r for r in env.received if "/data/" in r["path"]]


# --- monitoring layer: reads Superset metadata, never a client database ------

OPS_WATCH = """
name: slow-anywhere
description: Long-running queries anywhere on Tathya
source:
  type: ops
  signal: slow_queries
  since: 1h
  threshold_seconds: 30
condition:
  type: threshold
  op: ">"
  value: 0
schedule:
  every: 15m
notify:
  targets: [local]
"""


def test_ops_load_aggregates_all_databases(env, cli):
    """Observation covers every database, not just allowlisted ones."""
    code, payload = cli("ops", "load", "--since", "1h")
    assert code == EXIT_OK
    names = {d["database"] for d in payload["data"]["databases"]}
    assert "acme_oms" in names
    assert "examples" in names  # not allowlisted, still observed
    worst = payload["data"]["databases"][0]
    assert worst["database"] == "acme_oms"
    assert worst["max_seconds"] == 45.0


def test_ops_does_not_consume_the_query_budget(env, cli):
    """Monitoring must not eat the agent budget - it causes no replica load."""
    cli("ops", "load", "--since", "1h")
    cli("ops", "slow", "--since", "1h")
    cli("ops", "failures", "--since", "1h")
    cli("ops", "alerts")
    code, payload = cli("limits")
    assert payload["data"]["used_today"] == {}


def test_ops_window_filter_excludes_old_queries(env, cli):
    """A 3-day-old 90s query must not appear in a 1h window."""
    code, payload = cli("ops", "slow", "--since", "1h", "--min-seconds", "10")
    assert [q["seconds"] for q in payload["data"]["queries"]] == [45.0]


def test_ops_failures_groups_by_database(env, cli):
    code, payload = cli("ops", "failures", "--since", "1h")
    assert payload["data"]["failed_total"] == 2
    assert {d["database"] for d in payload["data"]["by_database"]} == {"acme_oms", "examples"}


def test_ops_database_filter_accepts_a_name(env, cli):
    code, payload = cli("ops", "load", "--since", "1h", "--database", "acme_oms")
    assert code == EXIT_OK
    assert {d["database"] for d in payload["data"]["databases"]} == {"acme_oms"}


def test_ops_alerts_classifies_every_report(env, cli):
    code, payload = cli("ops", "alerts")
    assert code == EXIT_OK
    counts = payload["data"]["counts"]
    assert set(counts) == {"error", "stale", "never_run", "healthy", "inactive"}
    assert counts["error"] == 1       # the Error-state alert
    assert counts["inactive"] == 1    # the switched-off report
    assert payload["data"]["total"] == 6


def test_digest_flags_attention_and_exits_9(env, cli):
    code, payload = cli("digest", "--since", "1h")
    assert code == EXIT_ALERT
    data = payload["data"]
    assert data["attention"]
    assert data["query_pressure"]["total_queries"] == 5
    assert any("acme_oms" in item for item in data["attention"])


def test_digest_can_be_forced_to_exit_zero(env, cli):
    code, _ = cli("digest", "--since", "1h", "--no-alert-exit")
    assert code == EXIT_OK


def test_digest_previews_chat_without_sending(env, cli):
    code, payload = cli("digest", "--since", "1h", "--target", "gchat", "--no-alert-exit")
    assert code == EXIT_OK
    delivery = payload["data"]["deliveries"][0]
    assert delivery["dry_run"] is True
    assert "Tathya production digest" in delivery["preview"]


def test_ops_backed_watch_evaluates_without_touching_a_database(env, cli):
    (env.watches / "slow.yaml").write_text(OPS_WATCH)
    code, payload = cli("watch", "run", "--all")
    assert code == EXIT_ALERT
    result = payload["data"]["results"][0]
    assert result["status"] == "alert"
    assert result["value"] == 1                     # the 45s query
    assert result["detail"]["cost"] == "superset metadata only"
    # No SQL Lab execution, and no budget consumed.
    assert not [r for r in env.received if r["path"] == "/api/v1/sqllab/execute/"]
    code2, limits = cli("limits")
    assert limits["data"]["used_today"] == {}


def test_ops_watch_rejects_a_rowcount_condition(env, cli):
    (env.watches / "bad.yaml").write_text(
        "name: bad\nsource:\n  type: ops\n  signal: failed_queries\n"
        "condition:\n  type: rowcount\n  op: '>'\n  value: 0\n"
    )
    code, payload = cli("watch", "list")
    assert code == EXIT_CONFIG
    assert "threshold, change, or delta_pct" in payload["error"]["message"]


# ------------------------------------------------------------- dataset writes
VIRTUAL_SQL = "SELECT order_id FROM order_header WHERE status_id = 'APPROVED'"


def _sql_file(tmp_path, sql):
    path = tmp_path / "dataset.sql"
    path.write_text(sql)
    return str(path)


def test_dataset_update_dry_run_shows_hashes(env, cli, tmp_path):
    new_sql = "SELECT order_id FROM order_header WHERE status_id = 'CREATED'"
    code, payload = cli("dataset", "update", "9", "--sql-file", _sql_file(tmp_path, new_sql))
    assert code == EXIT_OK
    data = payload["data"]
    assert data["dry_run"] is True
    assert data["summary"]["sql"]["new_length"] == len(new_sql)
    assert data["planned_calls"][0]["payload"]["sql"] == new_sql
    assert not env.received


def test_dataset_update_applies_with_both_switches(env, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    new_sql = "SELECT order_id FROM order_header WHERE status_id = 'CREATED'"
    code, payload = cli("dataset", "update", "9", "--sql-file", _sql_file(tmp_path, new_sql), "--confirm")
    assert code == EXIT_OK
    puts = [r for r in env.received if r["method"] == "PUT"]
    assert puts == [{"method": "PUT", "path": "/api/v1/dataset/9", "body": {"sql": new_sql}}]


def test_dataset_update_refuses_write_sql(env, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    code, payload = cli(
        "dataset", "update", "9", "--sql-file", _sql_file(tmp_path, "DELETE FROM order_header"), "--confirm"
    )
    assert code != EXIT_OK
    assert "guard" in payload["error"]["message"]
    assert not env.received


def test_dataset_update_refuses_stale_expected_hash(env, cli, tmp_path, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    code, payload = cli(
        "dataset", "update", "9", "--sql-file", _sql_file(tmp_path, "SELECT 1"),
        "--expect-sql-sha256", "deadbeef", "--confirm",
    )
    assert code != EXIT_OK
    assert "changed since it was read" in payload["error"]["message"]
    assert not env.received


def test_dataset_update_accepts_matching_expected_hash(env, cli, tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    current = hashlib.sha256(VIRTUAL_SQL.encode()).hexdigest()[:16]
    code, _ = cli(
        "dataset", "update", "9", "--sql-file", _sql_file(tmp_path, "SELECT 1 AS x"),
        "--expect-sql-sha256", current, "--confirm",
    )
    assert code == EXIT_OK
    assert [r["path"] for r in env.received if r["method"] == "PUT"] == ["/api/v1/dataset/9"]


def test_dataset_update_refuses_sql_on_physical_dataset(env, cli, tmp_path):
    code, payload = cli("dataset", "update", "7", "--sql-file", _sql_file(tmp_path, "SELECT 1"))
    assert code != EXIT_OK
    assert "not a virtual" in payload["error"]["message"]


def test_dataset_delete_blocked_without_allow_delete(env, cli, monkeypatch):
    monkeypatch.setenv("TATHYA_ALLOW_WRITE", "1")
    code, payload = cli("dataset", "delete", "9", "--confirm")
    assert code == EXIT_WRITE_BLOCKED
    assert not env.received
