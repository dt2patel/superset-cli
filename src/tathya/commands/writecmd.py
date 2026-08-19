"""Dashboard and chart management.

Every write is dry-run unless ``--confirm`` is passed, and ``--confirm`` alone is
not enough: ``TATHYA_ALLOW_WRITE=1`` must also be set in the environment. Two
independent switches means neither a stray flag in a generated command nor an
exported variable can change Tathya on its own.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from tathya.context import Context
from tathya.errors import UsageError
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
) -> int:
    """Run (or preview) an ordered list of mutating API calls."""
    if not ctx.confirmed:
        return ctx.emitter.ok(
            action,
            {"dry_run": True, "planned_calls": calls},
            warnings=[
                "Dry run - Tathya was not modified. Re-run with --confirm and "
                "TATHYA_ALLOW_WRITE=1 to apply."
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
            "calls": [{"method": c["method"], "path": c["path"]} for c in completed],
        },
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
