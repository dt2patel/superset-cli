"""Production monitoring commands.

These read Superset's own operational metadata. They deliberately observe **all**
databases, not just allowlisted ones: the allowlist governs what an agent may
*cause* load on, whereas these only read the record of load that has already
happened. Observation is not the same authority as execution.
"""

from __future__ import annotations

from tathya import ops
from tathya.context import Context
from tathya.errors import UsageError
from tathya.output import render_table
from tathya.rison import equals_filter, query as rison_query


def _resolve_database_filter(ctx: Context) -> tuple[int | None, str | None]:
    """Map a --database name to an id for server-side filtering."""
    name = getattr(ctx.args, "database", None)
    if not name:
        return None, None
    return ops.resolve_database_id_by_name(ctx, name), str(name)


def _window(ctx: Context) -> int:
    return ops.parse_window(getattr(ctx.args, "since", None) or ctx.config.digest.window)


def cmd_ops_load(ctx: Context) -> int:
    window = _window(ctx)
    db_id, db_name = _resolve_database_filter(ctx)
    rows, truncated = ops.fetch_queries(ctx, window_seconds=window, database_id=db_id)
    summary = ops.summarize_load(rows)
    summary["window_seconds"] = window
    summary["database_filter"] = db_name
    limit = ctx.args.top or ctx.config.digest.top_databases
    summary["databases"] = summary["databases"][:limit]

    warnings = []
    if truncated:
        warnings.append(
            f"Only the {ops.MAX_QUERY_ROWS} most recent queries were read; "
            "narrow the window for a complete picture."
        )

    def human(data, line):
        line(f"{data['total_queries']} queries in the last {getattr(ctx.args,'since',None) or ctx.config.digest.window}"
             f" · {data['failure_rate_pct']}% failed")
        line("")
        render_table(data["databases"],
                     ["database", "queries", "failures", "avg_seconds", "p95_seconds",
                      "max_seconds", "distinct_users", "top_user"], line)

    return ctx.emitter.ok("ops.load", summary, warnings=warnings, human=human)


def cmd_ops_slow(ctx: Context) -> int:
    window = _window(ctx)
    db_id, db_name = _resolve_database_filter(ctx)
    rows, _ = ops.fetch_queries(ctx, window_seconds=window, database_id=db_id)
    threshold = ctx.args.min_seconds if ctx.args.min_seconds is not None else ctx.config.digest.slow_query_seconds
    slow = ops.slowest(rows, min_seconds=threshold, limit=ctx.args.limit)

    def human(data, line):
        line(f"{len(data['queries'])} queries over {threshold:g}s "
             f"(of {data['scanned']} in the window)")
        line("")
        render_table(data["queries"], ["seconds", "database", "user", "status", "rows", "sql"], line)

    return ctx.emitter.ok(
        "ops.slow",
        {"queries": slow, "scanned": len(rows), "min_seconds": threshold,
         "database_filter": db_name, "window_seconds": window},
        human=human,
    )


def cmd_ops_failures(ctx: Context) -> int:
    window = _window(ctx)
    db_id, db_name = _resolve_database_filter(ctx)
    rows, _ = ops.fetch_queries(ctx, window_seconds=window, database_id=db_id)
    report = ops.failures(rows, limit=ctx.args.limit)
    report.update({"scanned": len(rows), "database_filter": db_name, "window_seconds": window})

    def human(data, line):
        line(f"{data['failed_total']} failed of {data['scanned']} queries in the window")
        line("")
        render_table(data["by_database"], ["database", "failures"], line)
        if data["samples"]:
            line("")
            line("recent examples:")
            render_table(data["samples"], ["database", "user", "status", "sql"], line)

    return ctx.emitter.ok("ops.failures", report, human=human)


def cmd_ops_queries(ctx: Context) -> int:
    window = _window(ctx)
    db_id, db_name = _resolve_database_filter(ctx)
    rows, truncated = ops.fetch_queries(
        ctx, window_seconds=window, database_id=db_id,
        status=ctx.args.status, sql_contains=ctx.args.sql_contains,
    )
    slim = ops.slowest(rows, min_seconds=0.0, limit=ctx.args.limit)

    def human(data, line):
        line(f"{data['total']} queries in the window"
             + (f" on {db_name}" if db_name else "")
             + (f" with status {ctx.args.status}" if ctx.args.status else ""))
        line("")
        render_table(data["queries"], ["seconds", "database", "user", "status", "rows", "sql"], line)

    return ctx.emitter.ok(
        "ops.queries",
        {"queries": slim, "total": len(rows), "database_filter": db_name,
         "window_seconds": window},
        warnings=["Result truncated; narrow the window."] if truncated else [],
        human=human,
    )


def cmd_ops_alerts(ctx: Context) -> int:
    """Health of Superset's own Alerts & Reports.

    A configured alert that silently stopped evaluating is worse than one that
    visibly errors, so staleness is surfaced as its own state.
    """
    health = ops.classify_reports(ops.fetch_reports(ctx))

    def human(data, line):
        counts = data["counts"]
        line(f"{data['total']} alerts & reports configured")
        line("  " + " · ".join(f"{k}={v}" for k, v in counts.items()))
        for label, key in (("in error", "error"), ("stale", "stale"), ("never run", "never_run")):
            items = data.get(key) or []
            if items:
                line("")
                line(f"{label} ({len(items)}):")
                render_table(items[:15], ["id", "name", "type", "last_state",
                                          "last_eval_age_hours", "schedule"], line)

    problems = len(health["error"]) + len(health["stale"]) + len(health["never_run"])
    warnings = ([f"{problems} alerts need attention."] if problems else [])
    return ctx.emitter.ok("ops.alerts", health, warnings=warnings, human=human)
