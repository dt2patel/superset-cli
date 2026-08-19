"""The single guarded path to the databases behind Tathya.

Both ``tathya sql run`` and the watch engine call through here, so the guard,
the allowlist, the rate limiter, and the audit record cannot be bypassed by
adding a new command later.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from tathya import guard
from tathya.audit import sql_fingerprint
from tathya.config import DatabaseRule, state_dir
from tathya.context import Context
from tathya.errors import ConfigError, GuardError, QueryTimeout, UpstreamError
from tathya.rison import equals_filter, query as rison_query

EXECUTE_PATH = "/api/v1/sqllab/execute/"
ESTIMATE_PATH = "/api/v1/sqllab/estimate/"

#: Superset stores this in ``Query.client_id``, declared as ``String(11)`` in
#: superset/models/sql_lab.py. A longer value fails the metadata-DB insert and
#: surfaces as an opaque ``HTTP 500 "Create failed" / GENERIC_DB_ENGINE_ERROR``
#: that says nothing about length. Verified against Tathya: 11 chars pass, 12 fail.
CLIENT_ID_MAX_CHARS = 11


def new_client_id() -> str:
    return uuid.uuid4().hex[:CLIENT_ID_MAX_CHARS]


def resolve_database_id(ctx: Context, rule: DatabaseRule) -> int:
    """Map an allowlisted database to its Superset id.

    A pinned ``id`` in config is trusted as-is; otherwise the name is looked up
    and the answer must be unique, because silently picking the first of several
    same-named databases could point a query at the wrong client.
    """
    if rule.id is not None:
        return rule.id
    payload = ctx.client.get(
        "/api/v1/database/",
        params={
            "q": rison_query(
                columns=["id", "database_name"],
                filters=[equals_filter("database_name", rule.name)],
                page=0,
                page_size=25,
            )
        },
    )
    matches = [
        row
        for row in (payload.get("result") or [])
        if str(row.get("database_name", "")).lower() == rule.name.lower()
    ]
    if not matches:
        raise ConfigError(
            f"Allowlisted database {rule.name!r} was not found on this Tathya instance.",
            hint="Check the name with `tathya db list`, or pin `id` in the config.",
        )
    if len(matches) > 1:
        raise ConfigError(
            f"{len(matches)} databases are named {rule.name!r}; pin the right `id` in config.",
            candidates=[row.get("id") for row in matches],
        )
    return int(matches[0]["id"])


def _rows_from_execute(payload: dict) -> tuple[list[dict], list[str]]:
    data = payload.get("data")
    if data is None:
        raise UpstreamError(
            "Superset accepted the query but returned no data block.",
            status_field=payload.get("status"),
            hint="If asynchronous queries are enabled on Tathya, results may need polling.",
        )
    columns = [
        col.get("name") if isinstance(col, dict) else str(col)
        for col in (payload.get("columns") or [])
    ]
    if not columns and data:
        columns = list(data[0].keys())
    return data, columns


def estimate(ctx: Context, *, database_ref: str, sql: str, schema: str | None = None) -> dict:
    """Ask the engine to cost a query without running it.

    Support is engine-dependent (Presto/Trino and BigQuery report real costs;
    others return an error), so a failure here is reported, not fatal.
    """
    rule = ctx.config.database_rule(database_ref)
    verdict = guard.analyze(
        sql,
        max_rows=ctx.config.row_cap_for(rule),
        dialect=rule.dialect,
        denied_functions=ctx.config.safety.denied_functions,
    )
    if not verdict.ok:
        raise GuardError(verdict.reason or "Statement refused.", rule=verdict.rule)
    database_id = resolve_database_id(ctx, rule)
    body: dict[str, Any] = {"database_id": database_id, "sql": verdict.sql}
    target_schema = schema or rule.default_schema
    if target_schema:
        body["schema"] = target_schema
    try:
        payload = ctx.client.post(ESTIMATE_PATH, json_body=body)
    except UpstreamError as exc:
        return {
            "database": rule.name,
            "supported": False,
            "detail": exc.message,
            "guard": verdict.to_dict(),
        }
    return {
        "database": rule.name,
        "supported": True,
        "estimate": payload.get("result", payload),
        "guard": verdict.to_dict(),
    }


def run_sql(
    ctx: Context,
    *,
    database_ref: str,
    sql: str,
    schema: str | None = None,
    dialect: str | None = None,
    max_rows: int | None = None,
    command_label: str = "sql.run",
) -> dict:
    """Guard, rate-limit, execute, and audit one read-only statement.

    ``max_rows`` may only *tighten* the configured cap. A caller asking for more
    rows than config allows gets the config value, so no command-line flag or
    watch file can widen the blast radius.
    """
    rule = ctx.config.database_rule(database_ref)
    row_cap = ctx.config.row_cap_for(rule)
    if max_rows is not None:
        row_cap = max(1, min(row_cap, int(max_rows)))
    verdict = guard.analyze(
        sql,
        max_rows=row_cap,
        dialect=dialect or rule.dialect,
        denied_functions=ctx.config.safety.denied_functions,
    )
    fingerprint = sql_fingerprint(sql)

    if not verdict.ok:
        ctx.audit.record(
            command=command_label,
            database=rule.name,
            sql=sql,
            sql_sha256=fingerprint,
            guard=verdict.to_dict(),
            outcome="guard_rejected",
        )
        raise GuardError(
            verdict.reason or "Statement refused by the SQL guard.",
            rule=verdict.rule,
            database=rule.name,
        )

    database_id = resolve_database_id(ctx, rule)
    body: dict[str, Any] = {
        "database_id": database_id,
        "sql": verdict.sql,
        "queryLimit": verdict.applied_limit or row_cap,
        "runAsync": False,
        "expand_data": False,
        "client_id": new_client_id(),
    }
    target_schema = schema or rule.default_schema
    if target_schema:
        body["schema"] = target_schema

    started = time.perf_counter()
    try:
        with ctx.limiter.acquire(f"db:{rule.name}"):
            payload = ctx.client.post(
                EXECUTE_PATH,
                json_body=body,
                timeout=ctx.config.safety.query_timeout_seconds,
            )
    except Exception as exc:
        ctx.audit.record(
            command=command_label,
            database=rule.name,
            sql=verdict.sql,
            sql_sha256=fingerprint,
            guard=verdict.to_dict(),
            duration_ms=round((time.perf_counter() - started) * 1000),
            outcome=type(exc).__name__,
            error=str(getattr(exc, "message", exc)),
        )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000)
    rows, columns = _rows_from_execute(payload)
    result = {
        "database": rule.name,
        "database_id": database_id,
        "schema": target_schema,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) >= (verdict.applied_limit or row_cap),
        "duration_ms": duration_ms,
        "guard": verdict.to_dict(),
        "query_id": payload.get("query_id"),
    }
    ctx.audit.record(
        command=command_label,
        database=rule.name,
        sql=verdict.sql,
        sql_sha256=fingerprint,
        guard=verdict.to_dict(),
        rows=len(rows),
        duration_ms=duration_ms,
        outcome="success",
    )
    return result


CHART_DB_CACHE = "chart-database.json"


def _chart_db_cache() -> tuple[Path, dict]:
    path = state_dir() / CHART_DB_CACHE
    try:
        return path, json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return path, {}


def _datasource_id_from_params(chart: dict) -> int | None:
    """Chart params carry the datasource as "2223__table"."""
    raw = chart.get("params")
    if not isinstance(raw, str):
        return None
    try:
        datasource = json.loads(raw).get("datasource")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(datasource, str):
        return None
    head = datasource.split("__", 1)[0]
    return int(head) if head.isdigit() else None


def resolve_chart_database(ctx: Context, chart_id: int) -> str:
    """Which database would running this chart put load on?

    A chart carries no database of its own: it points at a dataset, and the
    dataset points at a database. Without walking that chain there is no name to
    check the allowlist against - which is how saved-chart queries used to
    bypass it entirely.

    Superset's chart *detail* endpoint omits ``datasource_id``; only the *list*
    endpoint returns it, with the id also embedded in ``params.datasource`` as
    ``"<id>__table"``. Both are tried before giving up. The mapping never
    changes, so the answer is cached.
    """
    path, cache = _chart_db_cache()
    cached = cache.get(str(chart_id))
    if cached:
        return cached

    dataset_id: int | None = None
    listing = ctx.client.get(
        "/api/v1/chart/",
        params={
            "q": rison_query(
                columns=["id", "datasource_id"],
                filters=[equals_filter("id", int(chart_id))],
                page=0,
                page_size=1,
            )
        },
    )
    # Verify the row really is the chart asked for. If the server ignores the
    # filter, taking whatever came back first would attribute this chart to a
    # different chart's dataset - and therefore to the wrong client's database.
    for row in (listing or {}).get("result") or []:
        try:
            same_chart = int(row.get("id")) == int(chart_id)
        except (TypeError, ValueError):
            same_chart = False
        if same_chart and row.get("datasource_id"):
            dataset_id = int(row["datasource_id"])
            break

    if dataset_id is None:
        chart = (ctx.client.get(f"/api/v1/chart/{chart_id}") or {}).get("result") or {}
        dataset_id = chart.get("datasource_id") or _datasource_id_from_params(chart)

    if not dataset_id:
        raise ConfigError(
            f"Could not determine which dataset chart {chart_id} uses, so the database it "
            "would query is unknown - refusing rather than running it unchecked.",
            chart_id=chart_id,
        )

    dataset = (ctx.client.get(f"/api/v1/dataset/{dataset_id}") or {}).get("result") or {}
    name = ((dataset.get("database") or {}).get("database_name") or "").strip()
    if not name:
        raise ConfigError(
            f"Could not determine the database behind chart {chart_id} (dataset "
            f"{dataset_id}) - refusing rather than running it unchecked.",
            chart_id=chart_id,
            dataset_id=dataset_id,
        )

    cache[str(chart_id)] = name
    try:
        path.write_text(json.dumps(cache, indent=2))
        path.chmod(0o600)
    except OSError:
        pass  # a cache write failure must not block the query
    return name


def chart_data(ctx: Context, chart_id: int, *, command_label: str = "chart.data") -> dict:
    """Fetch a saved chart's data.

    No SQL guard applies - the query is Superset's own, built and saved by a
    person - but it still executes against a client database, so it must clear
    the same allowlist and rate-limit budget as ad-hoc SQL.
    """
    database = resolve_chart_database(ctx, chart_id)
    rule = ctx.config.database_rule(database)  # exit 3 when not allowlisted

    started = time.perf_counter()
    try:
        with ctx.limiter.acquire(f"db:{rule.name}"):
            try:
                payload = ctx.client.get(f"/api/v1/chart/{chart_id}/data/")
            except UpstreamError as exc:
                # Superset can only serve this endpoint for charts that have a
                # saved query_context. Charts created by older versions or via the
                # API often lack it, and the raw error does not say what to do.
                if "query context" in str(exc.message).lower():
                    raise UpstreamError(
                        f"Chart {chart_id} has no saved query context, so Superset cannot "
                        "run it through the API.",
                        chart_id=chart_id,
                        database=rule.name,
                        fix="Open the chart in Tathya and save it once to populate its "
                            "query context, or use `tathya sql run` instead.",
                    ) from exc
                raise
    except Exception as exc:
        ctx.audit.record(
            command=command_label,
            chart_id=chart_id,
            database=rule.name,
            outcome=type(exc).__name__,
            error=str(getattr(exc, "message", exc)),
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000)
    blocks = payload.get("result") or []
    first = blocks[0] if blocks else {}
    rows = first.get("data") or []
    ctx.audit.record(
        command=command_label,
        chart_id=chart_id,
        database=rule.name,
        rows=len(rows),
        duration_ms=duration_ms,
        outcome="success",
    )
    return {
        "chart_id": chart_id,
        "database": rule.name,
        "rows": rows,
        "row_count": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "duration_ms": duration_ms,
        "query_count": len(blocks),
    }
