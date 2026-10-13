"""Dashboard and chart management.

Every write is dry-run unless ``--confirm`` is passed, and ``--confirm`` alone is
not enough: ``TATHYA_ALLOW_WRITE=1`` must also be set in the environment. Two
independent switches means neither a stray flag in a generated command nor an
exported variable can change Tathya on its own.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from tathya import guard
from tathya.config import BACKEND_TO_DIALECT
from tathya.context import Context
from tathya.errors import ConfigError, UsageError
from tathya.output import render_table

GRID_ID = "GRID_ID"
ROOT_ID = "ROOT_ID"


def _load_json_file(value: str | None, label: str) -> Any:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.exists():
        raise UsageError(f"{label} file not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise UsageError(f"{label} file is not valid JSON: {exc}") from exc


def _apply(
    ctx: Context,
    action: str,
    calls: list[dict],
    *,
    destructive: bool = False,
    warnings: list[str] | None = None,
    summary: dict | None = None,
) -> int:
    """Run (or preview) an ordered list of mutating API calls."""
    extra = {"summary": summary} if summary else {}
    if not ctx.confirmed:
        return ctx.emitter.ok(
            action,
            {"dry_run": True, **extra, "planned_calls": calls},
            warnings=[
                *(warnings or []),
                "Dry run - Tathya was not modified. Re-run with --confirm and "
                "TATHYA_ALLOW_WRITE=1 to apply.",
            ],
        )
    ctx.check_write(action, destructive=destructive)

    completed: list[dict] = []
    for index, call in enumerate(calls):
        try:
            response = ctx.client.request(
                call["method"], call["path"], json_body=call.get("payload")
            )
        except Exception as exc:
            ctx.audit.record(
                command=action,
                step=index,
                path=call["path"],
                method=call["method"],
                payload=call.get("payload"),
                outcome="failed",
                error=str(getattr(exc, "message", exc)),
                completed_steps=len(completed),
            )
            if completed:
                # Be explicit that the change landed only partially.
                exc.details = {  # type: ignore[attr-defined]
                    **getattr(exc, "details", {}),
                    "partial_change": True,
                    "completed_steps": [c["path"] for c in completed],
                }
            raise
        completed.append(call)
        ctx.audit.record(
            command=action,
            step=index,
            path=call["path"],
            method=call["method"],
            payload=call.get("payload"),
            outcome="success",
        )
    return ctx.emitter.ok(
        action,
        {
            "applied": True,
            **extra,
            "calls": [{"method": c["method"], "path": c["path"]} for c in completed],
        },
        warnings=list(warnings or []),
    )


# ----------------------------------------------------------------------- charts
def cmd_chart_create(ctx: Context) -> int:
    params = _load_json_file(ctx.args.params_file, "--params-file") or {}
    payload: dict[str, Any] = {
        "slice_name": ctx.args.name,
        "datasource_id": ctx.args.dataset_id,
        "datasource_type": "table",
        "viz_type": ctx.args.viz_type,
        "params": json.dumps(params),
    }
    if ctx.args.description:
        payload["description"] = ctx.args.description
    if ctx.args.dashboard_ids:
        payload["dashboards"] = [int(v) for v in ctx.args.dashboard_ids]
    return _apply(ctx, "chart.create", [{"method": "POST", "path": "/api/v1/chart/", "payload": payload}])


def cmd_chart_update(ctx: Context) -> int:
    payload: dict[str, Any] = {}
    if ctx.args.name:
        payload["slice_name"] = ctx.args.name
    if ctx.args.viz_type:
        payload["viz_type"] = ctx.args.viz_type
    if ctx.args.description is not None:
        payload["description"] = ctx.args.description
    params = _load_json_file(ctx.args.params_file, "--params-file")
    if params is not None:
        payload["params"] = json.dumps(params)
    if ctx.args.dashboard_ids:
        payload["dashboards"] = [int(v) for v in ctx.args.dashboard_ids]
    if not payload:
        raise UsageError("Nothing to update; pass at least one field to change.")
    return _apply(
        ctx,
        "chart.update",
        [{"method": "PUT", "path": f"/api/v1/chart/{ctx.args.chart_id}", "payload": payload}],
    )


def cmd_chart_delete(ctx: Context) -> int:
    return _apply(
        ctx,
        "chart.delete",
        [{"method": "DELETE", "path": f"/api/v1/chart/{ctx.args.chart_id}"}],
        destructive=True,
    )


# --------------------------------------------------------------------- datasets
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_sql_file(value: str) -> str:
    path = Path(value).expanduser()
    if not path.exists():
        raise UsageError(f"--sql-file not found: {path}")
    sql = path.read_text()
    if not sql.strip():
        raise UsageError(f"--sql-file is empty: {path}")
    return sql


def _dialect_for(ctx: Context, database: dict) -> str | None:
    """sqlglot dialect for a dataset's database: the allowlist entry, else the backend."""
    try:
        rule = ctx.config.database_rule(database.get("database_name") or database.get("id"))
        if rule.dialect:
            return rule.dialect
    except ConfigError:
        pass
    return BACKEND_TO_DIALECT.get((database.get("backend") or "").lower())


def _guard_dataset_sql(ctx: Context, sql: str, dialect: str | None) -> list[str]:
    """Refuse dataset SQL the read-only guard would refuse in `sql run`.

    A virtual dataset's SQL runs against the client database every time a chart
    on it renders, so it gets the same read-only check as an ad-hoc query. The
    guard's LIMIT rewrite is not saved: charts apply their own row limits.
    """
    verdict = guard.analyze(
        sql,
        max_rows=ctx.config.safety.hard_max_rows,
        dialect=dialect,
        denied_functions=ctx.config.safety.denied_functions,
    )
    if not verdict.ok:
        raise UsageError(
            f"Dataset SQL refused by the SQL guard: {verdict.reason}",
            rule=verdict.rule,
            hint="Dataset SQL must be a single read-only SELECT/WITH statement.",
        )
    return list(verdict.warnings)


def cmd_dataset_create(ctx: Context) -> int:
    database = (ctx.client.get(f"/api/v1/database/{ctx.args.database_id}") or {}).get("result") or {}
    sql = _read_sql_file(ctx.args.sql_file)
    warnings = _guard_dataset_sql(ctx, sql, _dialect_for(ctx, database))
    payload: dict[str, Any] = {
        "database": ctx.args.database_id,
        "table_name": ctx.args.name,
        "sql": sql,
    }
    if ctx.args.schema:
        payload["schema"] = ctx.args.schema
    calls = [{"method": "POST", "path": "/api/v1/dataset/", "payload": payload}]
    return _apply(ctx, "dataset.create", calls, warnings=warnings)


def cmd_dataset_update(ctx: Context) -> int:
    dataset_id = ctx.args.dataset_id
    current = (ctx.client.get(f"/api/v1/dataset/{dataset_id}") or {}).get("result") or {}
    current_sql = current.get("sql") or ""
    payload: dict[str, Any] = {}
    warnings: list[str] = []
    summary: dict[str, Any] = {
        "dataset_id": dataset_id,
        "name": current.get("table_name"),
        "database": (current.get("database") or {}).get("database_name"),
    }

    if ctx.args.sql_file:
        if current.get("kind") == "physical" or not current_sql:
            raise UsageError(
                f"Dataset {dataset_id} is not a virtual (SQL) dataset; refusing to give it SQL.",
            )
        current_sha = _sha256(current_sql)
        # Optimistic concurrency: the caller states which SQL it read, so a change
        # someone made in the UI since then is never silently overwritten.
        if ctx.args.expect_sql_sha256 and not current_sha.startswith(ctx.args.expect_sql_sha256.lower()):
            raise UsageError(
                f"Dataset {dataset_id} SQL changed since it was read; refusing to overwrite it.",
                current_sql_sha256=current_sha,
                expected=ctx.args.expect_sql_sha256,
            )
        sql = _read_sql_file(ctx.args.sql_file)
        warnings += _guard_dataset_sql(ctx, sql, _dialect_for(ctx, current.get("database") or {}))
        if sql == current_sql:
            warnings.append("New SQL is identical to the current SQL; it was left out of the update.")
        else:
            payload["sql"] = sql
        summary["sql"] = {
            "current_sha256": current_sha,
            "current_length": len(current_sql),
            "new_sha256": _sha256(sql),
            "new_length": len(sql),
        }
    if ctx.args.name:
        payload["table_name"] = ctx.args.name
    if ctx.args.description is not None:
        payload["description"] = ctx.args.description
    if ctx.args.cache_timeout is not None:
        payload["cache_timeout"] = ctx.args.cache_timeout
    if not payload:
        raise UsageError("Nothing to update; pass at least one field that differs from the current value.")

    calls = [{"method": "PUT", "path": f"/api/v1/dataset/{dataset_id}", "payload": payload}]
    return _apply(ctx, "dataset.update", calls, warnings=warnings, summary=summary)


def cmd_dataset_delete(ctx: Context) -> int:
    return _apply(
        ctx,
        "dataset.delete",
        [{"method": "DELETE", "path": f"/api/v1/dataset/{ctx.args.dataset_id}"}],
        destructive=True,
    )


# ------------------------------------------------------------------- dashboards
def cmd_dashboard_create(ctx: Context) -> int:
    payload: dict[str, Any] = {"dashboard_title": ctx.args.title}
    if ctx.args.slug:
        payload["slug"] = ctx.args.slug
    payload["published"] = bool(ctx.args.published)
    metadata = _load_json_file(ctx.args.json_metadata_file, "--json-metadata-file")
    if metadata is not None:
        payload["json_metadata"] = json.dumps(metadata)
    position = _load_json_file(ctx.args.position_json_file, "--position-json-file")
    if position is not None:
        payload["position_json"] = json.dumps(position)
    return _apply(
        ctx, "dashboard.create", [{"method": "POST", "path": "/api/v1/dashboard/", "payload": payload}]
    )


def cmd_dashboard_update(ctx: Context) -> int:
    payload: dict[str, Any] = {}
    if ctx.args.title:
        payload["dashboard_title"] = ctx.args.title
    if ctx.args.slug:
        payload["slug"] = ctx.args.slug
    if ctx.args.published is not None:
        payload["published"] = ctx.args.published
    metadata = _load_json_file(ctx.args.json_metadata_file, "--json-metadata-file")
    if metadata is not None:
        payload["json_metadata"] = json.dumps(metadata)
    position = _load_json_file(ctx.args.position_json_file, "--position-json-file")
    if position is not None:
        payload["position_json"] = json.dumps(position)
    if not payload:
        raise UsageError("Nothing to update; pass at least one field to change.")
    return _apply(
        ctx,
        "dashboard.update",
        [{"method": "PUT", "path": f"/api/v1/dashboard/{ctx.args.dashboard_id}", "payload": payload}],
    )


def cmd_dashboard_delete(ctx: Context) -> int:
    return _apply(
        ctx,
        "dashboard.delete",
        [{"method": "DELETE", "path": f"/api/v1/dashboard/{ctx.args.dashboard_id}"}],
        destructive=True,
    )


def _empty_layout(title: str) -> dict:
    return {
        "DASHBOARD_VERSION_KEY": "v2",
        ROOT_ID: {"type": "ROOT", "id": ROOT_ID, "children": [GRID_ID]},
        GRID_ID: {"type": "GRID", "id": GRID_ID, "children": [], "parents": [ROOT_ID]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": title}},
    }


def cmd_dashboard_add_chart(ctx: Context) -> int:
    """Append a chart to a dashboard's grid.

    Superset stores layout in ``position_json`` and the dashboard/chart link on
    the chart record, so both have to be updated: position alone renders nothing
    in some views, association alone leaves the chart unplaced.
    """
    dashboard_id = ctx.args.dashboard_id
    chart_id = ctx.args.chart_id

    dash = (ctx.client.get(f"/api/v1/dashboard/{dashboard_id}") or {}).get("result") or {}
    chart = (ctx.client.get(f"/api/v1/chart/{chart_id}") or {}).get("result") or {}
    chart_name = chart.get("slice_name") or f"chart {chart_id}"

    raw_position = dash.get("position_json")
    if raw_position:
        try:
            layout = json.loads(raw_position)
        except json.JSONDecodeError as exc:
            raise UsageError(
                f"Dashboard {dashboard_id} has unparseable position_json; refusing to overwrite it: {exc}"
            ) from exc
    else:
        layout = _empty_layout(dash.get("dashboard_title") or f"dashboard {dashboard_id}")

    grid = layout.get(GRID_ID)
    if not isinstance(grid, dict):
        raise UsageError(
            f"Dashboard {dashboard_id} layout has no {GRID_ID} node; edit it in the UI instead.",
        )

    existing = [
        node
        for node in layout.values()
        if isinstance(node, dict)
        and node.get("type") == "CHART"
        and (node.get("meta") or {}).get("chartId") == chart_id
    ]
    if existing and not ctx.args.allow_duplicate:
        raise UsageError(
            f"Chart {chart_id} is already placed on dashboard {dashboard_id}.",
            hint="Pass --allow-duplicate to add it a second time.",
        )

    suffix = uuid.uuid4().hex[:10]
    row_id, chart_node_id = f"ROW-{suffix}", f"CHART-{suffix}"
    layout[row_id] = {
        "type": "ROW",
        "id": row_id,
        "children": [chart_node_id],
        "parents": [ROOT_ID, GRID_ID],
        "meta": {"background": "BACKGROUND_TRANSPARENT"},
    }
    layout[chart_node_id] = {
        "type": "CHART",
        "id": chart_node_id,
        "children": [],
        "parents": [ROOT_ID, GRID_ID, row_id],
        "meta": {
            "chartId": chart_id,
            "sliceName": chart_name,
            "width": ctx.args.width,
            "height": ctx.args.height,
            "uuid": chart.get("uuid"),
        },
    }
    grid.setdefault("children", []).append(row_id)

    linked = {d.get("id") for d in (chart.get("dashboards") or []) if isinstance(d, dict)}
    linked.add(dashboard_id)

    calls = [
        {
            "method": "PUT",
            "path": f"/api/v1/dashboard/{dashboard_id}",
            "payload": {"position_json": json.dumps(layout)},
        },
        {
            "method": "PUT",
            "path": f"/api/v1/chart/{chart_id}",
            "payload": {"dashboards": sorted(i for i in linked if i is not None)},
        },
    ]
    return _apply(ctx, "dashboard.add-chart", calls)
