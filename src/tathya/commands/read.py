"""Read-only commands: databases, datasets, charts, dashboards, exports."""

from __future__ import annotations

from pathlib import Path

from tathya import query as query_mod
from tathya.context import Context
from tathya.errors import UsageError
from tathya.output import render_table
from tathya.rison import contains_filter, query as rison_query

DASHBOARD_COLUMNS = ["id", "dashboard_title", "status", "changed_on_delta_humanized"]
CHART_COLUMNS = ["id", "slice_name", "viz_type", "datasource_id", "changed_on_delta_humanized"]
DATASET_COLUMNS = ["id", "table_name", "schema", "kind"]
DATABASE_COLUMNS = ["id", "database_name", "backend", "expose_in_sqllab", "allow_dml"]


def _listing(
    ctx: Context,
    path: str,
    columns: list[str],
    *,
    search_column: str | None = None,
    order_column: str | None = None,
):
    filters = []
    search = getattr(ctx.args, "search", None)
    if search and search_column:
        filters.append(contains_filter(search_column, search))
    page_size = min(int(getattr(ctx.args, "limit", 100)), 100)

    def builder(page: int, size: int) -> str:
        return rison_query(
            columns=columns,
            filters=filters or None,
            order_column=order_column,
            order_direction="asc" if order_column else None,
            page=page,
            page_size=size,
        )

    if getattr(ctx.args, "all", False):
        rows, total = ctx.client.list_all(path, q_builder=builder, page_size=page_size)
    else:
        payload = ctx.client.get(path, params={"q": builder(0, page_size)})
        rows, total = payload.get("result") or [], payload.get("count", 0)
    return rows, total


def _table_human(columns: list[str]):
    def human(data, line):
        rows = data["results"] if isinstance(data, dict) else data
        render_table(rows, columns, line)
        if isinstance(data, dict):
            line("")
            line(f"showing {len(rows)} of {data.get('total')} total")

    return human


def cmd_db_list(ctx: Context) -> int:
    rows, total = _listing(ctx, "/api/v1/database/", DATABASE_COLUMNS, search_column="database_name")
    allowed = {rule.name.lower() for rule in ctx.config.databases}
    for row in rows:
        row["agent_allowlisted"] = str(row.get("database_name", "")).lower() in allowed
    columns = DATABASE_COLUMNS + ["agent_allowlisted"]
    warnings = []
    if not allowed:
        warnings.append(
            "No databases are allowlisted, so `tathya sql run` will refuse every query. "
            "Add [[databases]] entries to the config."
        )
    return ctx.emitter.ok(
        "db.list",
        {"results": rows, "total": total},
        warnings=warnings,
        human=_table_human(columns),
    )


def cmd_dataset_list(ctx: Context) -> int:
    rows, total = _listing(
        ctx, "/api/v1/dataset/", DATASET_COLUMNS, search_column="table_name", order_column="table_name"
    )
    return ctx.emitter.ok(
        "dataset.list", {"results": rows, "total": total}, human=_table_human(DATASET_COLUMNS)
    )


def cmd_dataset_get(ctx: Context) -> int:
    payload = ctx.client.get(f"/api/v1/dataset/{ctx.args.dataset_id}")
    result = (payload or {}).get("result") or {}
    trimmed = {
        "id": ctx.args.dataset_id,
        "table_name": result.get("table_name"),
        "schema": result.get("schema"),
        "database": (result.get("database") or {}).get("database_name"),
        "sql": result.get("sql"),
        "columns": [
            {"name": col.get("column_name"), "type": col.get("type"), "is_dttm": col.get("is_dttm")}
            for col in (result.get("columns") or [])
        ],
        "metrics": [m.get("metric_name") for m in (result.get("metrics") or [])],
    }
    if ctx.args.raw:
        trimmed["raw"] = result

    def human(data, line):
        line(f"dataset {data['id']}: {data['table_name']} (schema={data['schema']}, db={data['database']})")
        line("")
        render_table(data["columns"], ["name", "type", "is_dttm"], line)
        if data["metrics"]:
            line("")
            line("metrics: " + ", ".join(m for m in data["metrics"] if m))

    return ctx.emitter.ok("dataset.get", trimmed, human=human)


def cmd_chart_list(ctx: Context) -> int:
    rows, total = _listing(ctx, "/api/v1/chart/", CHART_COLUMNS, search_column="slice_name")
    return ctx.emitter.ok(
        "chart.list", {"results": rows, "total": total}, human=_table_human(CHART_COLUMNS)
    )


def cmd_chart_get(ctx: Context) -> int:
    payload = ctx.client.get(f"/api/v1/chart/{ctx.args.chart_id}")
    return ctx.emitter.ok("chart.get", (payload or {}).get("result"))


def cmd_chart_data(ctx: Context) -> int:
    result = query_mod.chart_data(ctx, ctx.args.chart_id)

    def human(data, line):
        line(f"chart {data['chart_id']} on {data['database']}: "
             f"{data['row_count']} row(s) in {data['duration_ms']}ms")
        line("")
        render_table(data["rows"][:50], data["columns"], line)

    return ctx.emitter.ok("chart.data", result, human=human)


def cmd_dashboard_list(ctx: Context) -> int:
    rows, total = _listing(
        ctx, "/api/v1/dashboard/", DASHBOARD_COLUMNS, search_column="dashboard_title"
    )
    return ctx.emitter.ok(
        "dashboard.list", {"results": rows, "total": total}, human=_table_human(DASHBOARD_COLUMNS)
    )


def cmd_dashboard_get(ctx: Context) -> int:
    payload = ctx.client.get(f"/api/v1/dashboard/{ctx.args.dashboard}")
    return ctx.emitter.ok("dashboard.get", (payload or {}).get("result"))


def cmd_dashboard_charts(ctx: Context) -> int:
    payload = ctx.client.get(f"/api/v1/dashboard/{ctx.args.dashboard}/charts")
    rows = (payload or {}).get("result") or []
    slim = [
        {
            "slice_name": row.get("slice_name"),
            "viz_type": row.get("viz_type"),
            "slice_id": (row.get("form_data") or {}).get("slice_id"),
        }
        for row in rows
    ]

    def human(data, line):
        render_table(data, ["slice_id", "slice_name", "viz_type"], line)

    return ctx.emitter.ok("dashboard.charts", slim, human=human)


def cmd_dashboard_datasets(ctx: Context) -> int:
    payload = ctx.client.get(f"/api/v1/dashboard/{ctx.args.dashboard}/datasets")
    rows = (payload or {}).get("result") or []
    slim = [
        {
            "id": row.get("id"),
            "table_name": row.get("table_name"),
            "schema": row.get("schema"),
            "database": (row.get("database") or {}).get("database_name"),
        }
        for row in rows
    ]

    def human(data, line):
        render_table(data, ["id", "table_name", "schema", "database"], line)

    return ctx.emitter.ok("dashboard.datasets", slim, human=human)


def cmd_export(ctx: Context) -> int:
    """Export dashboards or charts as a ZIP of YAML.

    Superset masks database passwords in exports, but the YAML still describes
    every connection the asset touches. Review before committing anything.
    """
    kind = ctx.args.kind
    ids = [int(value) for value in ctx.args.ids]
    path = Path(ctx.args.out).expanduser()
    if path.exists() and not ctx.args.force:
        raise UsageError(f"{path} already exists; pass --force to overwrite.")
    from tathya.rison import dumps as rison_dumps

    content = ctx.client.get(
        f"/api/v1/{kind}/export/", params={"q": rison_dumps(ids)}, expect_json=False
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    ctx.audit.record(command=f"export.{kind}", ids=ids, bytes=len(content), outcome="success")
    return ctx.emitter.ok(
        "export",
        {"kind": kind, "ids": ids, "path": str(path), "bytes": len(content)},
        warnings=[
            "The ZIP contains database connection YAML. Superset masks DSN passwords, but "
            "usernames, hosts, ports, and database names are in the clear - review before "
            "committing to git."
        ],
    )
