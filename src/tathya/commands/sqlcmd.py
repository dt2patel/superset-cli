"""Ad-hoc SQL: the 'custom dataset answers' path, fully guarded."""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

from tathya import query as query_mod
from tathya.context import Context
from tathya.errors import UsageError
from tathya.output import render_table


def _read_sql(ctx: Context) -> str:
    sources = [bool(ctx.args.sql), bool(ctx.args.file), bool(ctx.args.stdin)]
    if sum(sources) != 1:
        raise UsageError("Provide exactly one of: SQL argument, --file, or --stdin.")
    if ctx.args.file:
        path = Path(ctx.args.file).expanduser()
        if not path.exists():
            raise UsageError(f"SQL file not found: {path}")
        return path.read_text()
    if ctx.args.stdin:
        return sys.stdin.read()
    return ctx.args.sql


def cmd_sql_run(ctx: Context) -> int:
    sql = _read_sql(ctx)

    if ctx.args.explain_only:
        # Show the guard's decision without touching the database at all.
        from tathya import guard

        rule = ctx.config.database_rule(ctx.args.database)
        cap = ctx.config.row_cap_for(rule)
        if ctx.args.max_rows:
            cap = max(1, min(cap, ctx.args.max_rows))
        verdict = guard.analyze(
            sql,
            max_rows=cap,
            dialect=ctx.args.dialect or rule.dialect,
            denied_functions=ctx.config.safety.denied_functions,
        )
        return ctx.emitter.ok(
            "sql.explain-only",
            {"database": rule.name, "would_execute": verdict.ok, "guard": verdict.to_dict()},
            warnings=list(verdict.warnings),
        )

    if ctx.args.estimate_first or ctx.config.safety.require_estimate:
        estimate = query_mod.estimate(
            ctx, database_ref=ctx.args.database, sql=sql, schema=ctx.args.schema
        )
        if ctx.args.estimate_only:
            return ctx.emitter.ok("sql.estimate", estimate)

    result = query_mod.run_sql(
        ctx,
        database_ref=ctx.args.database,
        sql=sql,
        schema=ctx.args.schema,
        dialect=ctx.args.dialect,
        max_rows=ctx.args.max_rows,
    )

    if ctx.args.format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=result["columns"])
        writer.writeheader()
        writer.writerows(result["rows"])
        print(buffer.getvalue(), end="")
        return 0

    def human(data, line):
        guard_info = data["guard"]
        line(
            f"{data['row_count']} row(s) from {data['database']} in {data['duration_ms']}ms "
            f"(limit {guard_info['applied_limit']}, {guard_info['limit_source']})"
        )
        line("")
        render_table(data["rows"], data["columns"], line)
        if data["truncated"]:
            line("")
            line("note: result hit the row cap and may be incomplete")

    return ctx.emitter.ok(
        "sql.run", result, warnings=list(result["guard"]["warnings"]), human=human
    )


def cmd_sql_estimate(ctx: Context) -> int:
    sql = _read_sql(ctx)
    estimate = query_mod.estimate(
        ctx, database_ref=ctx.args.database, sql=sql, schema=ctx.args.schema
    )
    warnings = (
        []
        if estimate.get("supported")
        else ["This database engine does not support cost estimation; nothing was measured."]
    )
    return ctx.emitter.ok("sql.estimate", estimate, warnings=warnings)
