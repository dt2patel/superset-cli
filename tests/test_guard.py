"""The guard is the last line before a client production replica. Test it hard."""

from __future__ import annotations

import pytest

from tathya.guard import analyze

DENIED = ("sleep", "pg_sleep", "benchmark", "waitfor")


def check(sql: str, **kwargs):
    return analyze(sql, max_rows=1000, dialect="postgres", denied_functions=DENIED, **kwargs)


@pytest.mark.parametrize(
    "sql,rule",
    [
        ("SELECT 1; DROP TABLE t", "multiple_statements"),
        ("DROP TABLE t", "not_a_read"),
        ("INSERT INTO t VALUES (1)", "not_a_read"),
        ("UPDATE t SET x = 1", "not_a_read"),
        ("DELETE FROM t", "not_a_read"),
        ("TRUNCATE TABLE t", "not_a_read"),
        ("GRANT ALL ON t TO u", "not_a_read"),
        ("SET statement_timeout = 0", "not_a_read"),
        ("COPY t TO '/tmp/x'", "not_a_read"),
        ("EXPLAIN SELECT 1", "not_a_read"),
        # Postgres data-modifying CTEs: the root really is a SELECT.
        ("WITH d AS (DELETE FROM t RETURNING id) SELECT * FROM d", "forbidden_node"),
        ("WITH u AS (UPDATE t SET x=1 RETURNING id) SELECT * FROM u", "forbidden_node"),
        ("WITH i AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM i", "forbidden_node"),
        # Parses as a Select, so a root type check alone would let it through.
        ("SELECT * INTO backup FROM t", "select_into"),
        ("SELECT * FROM t FOR UPDATE", "row_locks"),
        ("SELECT * FROM t FOR SHARE", "row_locks"),
        ("SELECT pg_sleep(600)", "denied_function"),
        ("SELECT benchmark(1000000, md5('x'))", "denied_function"),
        # sqlglot wraps syntax it cannot model in a Command node rather than
        # raising, so garbage lands on the not_a_read rule. Either way: refused.
        ("this is not sql", "not_a_read"),
        ("", "empty"),
    ],
)
def test_rejects(sql, rule):
    verdict = check(sql)
    assert not verdict.ok, f"guard allowed {sql!r}"
    assert verdict.rule == rule


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT a FROM t WHERE x = 1",
        "WITH c AS (SELECT 1 AS n) SELECT n FROM c",
        "SELECT 1 UNION SELECT 2",
        "SELECT COUNT(*) FROM t",
        "SELECT 1 -- ; DROP TABLE t",
        "SELECT/*hidden*/1",
        "SELECT a FROM t WHERE b IN (SELECT c FROM d)",
    ],
)
def test_allows_reads(sql):
    assert check(sql).ok, f"guard rejected legitimate read {sql!r}"


def test_limit_is_injected_when_absent():
    verdict = check("SELECT a FROM t")
    assert verdict.limit_source == "injected"
    assert verdict.applied_limit == 1000
    assert "LIMIT 1000" in verdict.sql.upper()


def test_limit_is_clamped_when_too_large():
    verdict = check("SELECT a FROM t LIMIT 999999")
    assert verdict.limit_source == "clamped"
    assert verdict.applied_limit == 1000
    assert "999999" not in verdict.sql


def test_small_limit_is_respected():
    verdict = check("SELECT a FROM t LIMIT 10")
    assert (verdict.limit_source, verdict.applied_limit) == ("existing", 10)


def test_non_literal_limit_is_clamped():
    """A limit the guard cannot read as a number must not be trusted."""
    verdict = analyze("SELECT a FROM t LIMIT ALL", max_rows=500, dialect="postgres")
    assert verdict.ok
    assert verdict.applied_limit == 500


def test_warns_on_unbounded_scan():
    verdict = check("SELECT * FROM order_header")
    assert any("WHERE" in w for w in verdict.warnings)
    assert any("all columns" in w for w in verdict.warnings)


def test_no_false_star_warning_for_count():
    verdict = check("SELECT COUNT(*) FROM t WHERE x = 1")
    assert not any("all columns" in w for w in verdict.warnings)


def test_warns_on_join_without_condition():
    assert any("cartesian" in w for w in check("SELECT a.x FROM a, b").warnings)


# --- Dialect handling (56 of Tathya's 79 databases are MySQL) --------------

def test_mysql_backticks_need_the_mysql_dialect():
    sql = "SELECT `order_id` FROM `order_header` WHERE `status_id` = 'X'"
    assert not analyze(sql, max_rows=100).ok            # generic parse fails closed
    verdict = analyze(sql, max_rows=100, dialect="mysql")
    assert verdict.ok
    assert "`order_id`" in verdict.sql                  # quoting preserved


def test_unparseable_rejection_names_the_dialect_used():
    verdict = analyze("SELECT `x` FROM `t`", max_rows=100)
    assert verdict.rule == "unparseable"
    assert "dialect" in (verdict.reason or "")


@pytest.mark.parametrize("placeholder", ["%s", "?", ":name", "@var"])
@pytest.mark.parametrize("dialect", [None, "mysql", "postgres"])
def test_placeholders_are_refused_in_every_dialect(placeholder, dialect):
    """Agents must send complete literal SQL; there is no binding step here."""
    verdict = analyze(
        f"SELECT order_id FROM order_header WHERE status_id = {placeholder}",
        max_rows=100,
        dialect=dialect,
    )
    assert not verdict.ok
    assert verdict.rule in {"placeholder", "unparseable"}


def test_literal_values_are_fine():
    assert analyze(
        "SELECT order_id FROM order_header WHERE status_id = 'APPROVED'",
        max_rows=100,
        dialect="mysql",
    ).ok


def test_guard_still_blocks_writes_under_mysql_dialect():
    for sql in ("DROP TABLE `t`", "DELETE FROM `t`", "SELECT * FROM `t` FOR UPDATE"):
        assert not analyze(sql, max_rows=100, dialect="mysql").ok
