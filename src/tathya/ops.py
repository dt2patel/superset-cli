"""Production monitoring built on Superset's own operational metadata.

Nothing here queries a client database. Superset already records every SQL Lab
query it has run - which database, which user, how long, whether it failed - and
the state of every configured Alert & Report. Reading that gives real visibility
into production load for the cost of a few metadata calls, instead of adding load
to the very replicas we are trying to protect.

That is the whole design premise: **monitor production without touching
production.** Consequently none of these commands consume the query rate-limit
budget, because they cannot put load on an OMS replica.
"""

from __future__ import annotations

import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from tathya.context import Context
from tathya.errors import ConfigError
from tathya.rison import query as rison_query

QUERY_PATH = "/api/v1/query/"
REPORT_PATH = "/api/v1/report/"

#: Hard ceiling on rows pulled per call. The metadata DB holds 150k+ query rows;
#: a time filter normally keeps this far smaller, but an unbounded sweep is never
#: the right default.
MAX_QUERY_ROWS = 2000
PAGE_SIZE = 100

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhdw]|min|hour|day|week)?\s*$", re.I)
_UNITS = {"m": 60, "min": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400,
          "w": 604800, "week": 604800}


def parse_window(text: str | int | float) -> int:
    """Accept ``30m``, ``6h``, ``2d``, ``1w``, or a bare number of minutes."""
    if isinstance(text, (int, float)):
        return int(text) * 60
    match = _DURATION_RE.match(str(text))
    if not match:
        raise ConfigError(f"Unparseable window {text!r}; use forms like 30m, 6h, 2d, 1w.")
    value, unit = float(match.group(1)), (match.group(2) or "m").lower()
    return int(value * _UNITS[unit])


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[index]


def resolve_database_id_by_name(ctx: Context, name: str) -> int:
    """Map a database name to its id, for *filtering observations*.

    This deliberately does not consult the allowlist: the allowlist governs what
    an agent may cause load on, while these commands only read the record of
    load that already happened. Observation is not execution.
    """
    from tathya.rison import equals_filter

    if str(name).isdigit():
        return int(name)
    payload = ctx.client.get(
        "/api/v1/database/",
        params={"q": rison_query(columns=["id", "database_name"],
                                 filters=[equals_filter("database_name", name)],
                                 page=0, page_size=5)},
    )
    matches = [r for r in (payload.get("result") or [])
               if str(r.get("database_name", "")).lower() == str(name).lower()]
    if not matches:
        raise ConfigError(f"No database named {name!r} on this instance.",
                          hint="Run `tathya db list` to see the exact names.")
    return int(matches[0]["id"])


# --------------------------------------------------------------------- fetching
def fetch_queries(
    ctx: Context,
    *,
    window_seconds: int,
    status: str | None = None,
    database_id: int | None = None,
    user_id: int | None = None,
    sql_contains: str | None = None,
    max_rows: int = MAX_QUERY_ROWS,
) -> tuple[list[dict], bool]:
    """Pull SQL Lab query history. Returns (rows, truncated).

    ``start_time`` is epoch milliseconds, and Superset filters it numerically, so
    the time window is applied server-side rather than by over-fetching.
    """
    since_ms = (time.time() - window_seconds) * 1000.0
    filters: list[dict] = [{"col": "start_time", "opr": "gt", "value": since_ms}]
    if status:
        filters.append({"col": "status", "opr": "eq", "value": status})
    if database_id is not None:
        filters.append({"col": "database", "opr": "rel_o_m", "value": int(database_id)})
    if user_id is not None:
        filters.append({"col": "user", "opr": "rel_o_m", "value": int(user_id)})
    if sql_contains:
        filters.append({"col": "sql", "opr": "ct", "value": sql_contains})

    rows: list[dict] = []
    truncated = False
    for page in range(max(1, max_rows // PAGE_SIZE)):
        payload = ctx.client.get(
            QUERY_PATH,
            params={
                "q": rison_query(
                    filters=filters,
                    order_column="start_time",
                    order_direction="desc",
                    page=page,
                    page_size=PAGE_SIZE,
                )
            },
        )
        chunk = payload.get("result") or []
        rows.extend(chunk)
        total = payload.get("count", len(rows))
        if len(chunk) < PAGE_SIZE or len(rows) >= total:
            break
        if len(rows) >= max_rows:
            truncated = len(rows) < total
            break
    return rows[:max_rows], truncated


def fetch_reports(ctx: Context, *, max_rows: int = 1000) -> list[dict]:
    """Pull every configured Alert & Report."""
    columns = ["id", "name", "type", "active", "last_state", "last_eval_dttm",
               "crontab", "crontab_humanized", "description", "timezone"]
    rows: list[dict] = []
    for page in range(max(1, max_rows // PAGE_SIZE)):
        payload = ctx.client.get(
            REPORT_PATH,
            params={"q": rison_query(columns=columns, page=page, page_size=PAGE_SIZE)},
        )
        chunk = payload.get("result") or []
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE or len(rows) >= payload.get("count", len(rows)):
            break
    return rows


# ------------------------------------------------------------------ aggregation
def _user_name(row: dict) -> str:
    user = row.get("user") or {}
    name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    return name or f"user {user.get('id', '?')}"


def _database_name(row: dict) -> str:
    return (row.get("database") or {}).get("database_name") or "(unknown)"


def _duration_seconds(row: dict) -> float | None:
    start, end = row.get("start_time"), row.get("end_time")
    if not start or not end:
        return None
    seconds = (float(end) - float(start)) / 1000.0
    return seconds if seconds >= 0 else None


@dataclass
class DatabaseLoad:
    database: str
    queries: int = 0
    failures: int = 0
    durations: list[float] = field(default_factory=list)
    users: Counter = field(default_factory=Counter)
    rows_returned: int = 0

    def to_dict(self) -> dict:
        return {
            "database": self.database,
            "queries": self.queries,
            "failures": self.failures,
            "failure_rate_pct": round(self.failures / self.queries * 100, 1) if self.queries else 0.0,
            "avg_seconds": round(statistics.fmean(self.durations), 2) if self.durations else None,
            "p95_seconds": round(_percentile(self.durations, 95), 2) if self.durations else None,
            "max_seconds": round(max(self.durations), 2) if self.durations else None,
            "rows_returned": self.rows_returned,
            "top_user": self.users.most_common(1)[0][0] if self.users else None,
            "distinct_users": len(self.users),
        }


def summarize_load(rows: Iterable[dict]) -> dict:
    """Per-database query pressure over the window."""
    per_db: dict[str, DatabaseLoad] = {}
    statuses: Counter = Counter()
    users: Counter = Counter()
    for row in rows:
        name = _database_name(row)
        load = per_db.setdefault(name, DatabaseLoad(database=name))
        load.queries += 1
        status = (row.get("status") or "").lower()
        statuses[status or "unknown"] += 1
        if status in {"failed", "timed_out", "stopped"}:
            load.failures += 1
        seconds = _duration_seconds(row)
        if seconds is not None:
            load.durations.append(seconds)
        who = _user_name(row)
        load.users[who] += 1
        users[who] += 1
        load.rows_returned += int(row.get("rows") or 0)

    ordered = sorted(
        (load.to_dict() for load in per_db.values()),
        key=lambda d: (-(d["max_seconds"] or 0), -d["queries"]),
    )
    total = sum(statuses.values())
    return {
        "total_queries": total,
        "statuses": dict(statuses.most_common()),
        "failure_rate_pct": round(
            sum(v for k, v in statuses.items() if k in {"failed", "timed_out", "stopped"})
            / total * 100, 1) if total else 0.0,
        "databases": ordered,
        "top_users": [{"user": u, "queries": n} for u, n in users.most_common(8)],
    }


def slowest(rows: Iterable[dict], *, min_seconds: float = 0.0, limit: int = 15) -> list[dict]:
    scored = []
    for row in rows:
        seconds = _duration_seconds(row)
        if seconds is None or seconds < min_seconds:
            continue
        sql = (row.get("executed_sql") or row.get("sql") or "").strip().replace("\n", " ")
        scored.append({
            "seconds": round(seconds, 2),
            "database": _database_name(row),
            "user": _user_name(row),
            "status": row.get("status"),
            "rows": row.get("rows"),
            "tables": row.get("sql_tables"),
            "sql": (sql[:160] + "...") if len(sql) > 160 else sql,
            "query_id": row.get("id"),
        })
    scored.sort(key=lambda d: -d["seconds"])
    return scored[:limit]


def failures(rows: Iterable[dict], *, limit: int = 20) -> dict:
    failed = [r for r in rows if (r.get("status") or "").lower() in {"failed", "timed_out", "stopped"}]
    by_db: Counter = Counter()
    by_user: Counter = Counter()
    samples: list[dict] = []
    for row in failed:
        by_db[_database_name(row)] += 1
        by_user[_user_name(row)] += 1
        if len(samples) < limit:
            sql = (row.get("executed_sql") or row.get("sql") or "").strip().replace("\n", " ")
            samples.append({
                "database": _database_name(row),
                "user": _user_name(row),
                "status": row.get("status"),
                "sql": (sql[:140] + "...") if len(sql) > 140 else sql,
                "query_id": row.get("id"),
            })
    return {
        "failed_total": len(failed),
        "by_database": [{"database": d, "failures": n} for d, n in by_db.most_common()],
        "by_user": [{"user": u, "failures": n} for u, n in by_user.most_common(8)],
        "samples": samples,
    }


# ---------------------------------------------------------------- alert health
#: Every state classify_reports can assign, in severity order.
REPORT_STATES = ("error", "stale", "never_run", "healthy", "inactive")

#: Fallback grace when an alert has no parseable schedule.
FLAT_GRACE_SECONDS = 3 * 3600

#: How late an alert may be before it counts as overdue. Alert evaluation is
#: queued, so it never fires exactly on the minute.
DEFAULT_GRACE_SECONDS = 3600


def _report_timezone(report: dict):
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = (report.get("timezone") or "").strip()
    if not name:
        return _tz.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return _tz.utc


def _parse_last_eval(report: dict):
    from datetime import datetime, timezone as _tz

    raw = report.get("last_eval_dttm")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    # Superset stores this naive, in UTC.
    return parsed.replace(tzinfo=_tz.utc) if parsed.tzinfo is None else parsed


def previous_expected_run(report: dict, now_utc, grace_seconds: int = DEFAULT_GRACE_SECONDS):
    """When this alert *should* most recently have evaluated.

    Judging staleness against a flat age is wrong: a monthly report legitimately
    sits idle for weeks, and a Friday-only alert for days. The schedule itself is
    the only correct yardstick, evaluated in the report's own timezone.
    """
    from datetime import datetime, timezone as _tz

    crontab = (report.get("crontab") or "").strip()
    if not crontab:
        return None
    try:
        from croniter import croniter

        tz = _report_timezone(report)
        local_now = now_utc.astimezone(tz)
        previous = croniter(crontab, local_now).get_prev(datetime)
        # If that slot is younger than the grace period, the run may simply not
        # have been picked off the queue yet - judge against the one before it.
        if (local_now - previous).total_seconds() < grace_seconds:
            previous = croniter(crontab, previous).get_prev(datetime)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=tz)
        return previous.astimezone(_tz.utc)
    except Exception:
        return None


def classify_reports(
    reports: list[dict],
    *,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    now=None,
) -> dict:
    """Sort Superset's own Alerts & Reports into states worth acting on.

    ``last_state`` is Superset's verdict: Success, Error, Grace, Working, or
    "Not triggered" (evaluated, condition false - healthy). Error is actionable.
    An active alert that has missed its own schedule is arguably worse, because
    it is silently protecting nothing.
    """
    from datetime import datetime, timezone as _tz

    # Injectable clock: staleness is schedule-relative, so it must be testable
    # against a pinned moment rather than whenever the suite happens to run.
    now = now or datetime.now(_tz.utc)
    buckets: dict[str, list[dict]] = defaultdict(list)
    unschedulable = 0

    for report in reports:
        state = str(report.get("last_state") or "never_run")
        last_eval = _parse_last_eval(report)
        age = (now - last_eval).total_seconds() if last_eval else None
        expected = previous_expected_run(report, now, grace_seconds)

        entry = {
            "id": report.get("id"),
            "name": report.get("name"),
            "type": report.get("type"),
            "active": bool(report.get("active")),
            "last_state": state,
            "schedule": report.get("crontab_humanized") or report.get("crontab"),
            "crontab": report.get("crontab"),
            "last_eval_age_hours": round(age / 3600, 1) if age is not None else None,
        }

        if not entry["active"]:
            buckets["inactive"].append(entry)
            continue
        if state.lower() == "error":
            buckets["error"].append(entry)
            continue
        if last_eval is None:
            buckets["never_run"].append(entry)
            continue
        if expected is None:
            unschedulable += 1
            if age is not None and age > FLAT_GRACE_SECONDS:
                entry["overdue_reason"] = "no parseable schedule; flat grace applied"
                buckets["stale"].append(entry)
            else:
                buckets["healthy"].append(entry)
            continue
        if last_eval < expected:
            entry["expected_last_run"] = expected.isoformat(timespec="minutes")
            entry["overdue_by_hours"] = round((now - expected).total_seconds() / 3600, 1)
            buckets["stale"].append(entry)
        else:
            buckets["healthy"].append(entry)

    buckets["error"].sort(key=lambda d: -(d["last_eval_age_hours"] or 0))
    buckets["stale"].sort(key=lambda d: -(d.get("overdue_by_hours") or 0))

    # Every state is always present, including zeros, so callers can read
    # counts["error"] without guarding for absence.
    return {
        "total": len(reports),
        "counts": {state: len(buckets[state]) for state in REPORT_STATES},
        "error": buckets["error"],
        "stale": buckets["stale"],
        "never_run": buckets["never_run"],
        "inactive_count": len(buckets["inactive"]),
        "healthy_count": len(buckets["healthy"]),
        "grace_hours": grace_seconds / 3600,
        "unschedulable": unschedulable,
    }


# ------------------------------------------------------- signals used by watches
def ops_signal(ctx: Context, signal: str, *, window_seconds: int, **opts: Any) -> tuple[float, str]:
    """Compute one numeric monitoring signal. Returns (value, human summary)."""
    if signal == "alert_errors":
        health = classify_reports(fetch_reports(ctx))
        count = len(health["error"])
        return count, f"{count} of {health['total']} alerts in error state"

    if signal == "alert_stale":
        health = classify_reports(fetch_reports(ctx))
        count = len(health["stale"])
        return count, f"{count} active alerts have missed their scheduled run"

    database_id = opts.get("database_id")
    rows, _ = fetch_queries(ctx, window_seconds=window_seconds, database_id=database_id)
    scope = opts.get("database") or "all databases"

    if signal == "failed_queries":
        count = failures(rows)["failed_total"]
        return count, f"{count} failed queries on {scope} in the window"
    if signal == "query_volume":
        return len(rows), f"{len(rows)} queries on {scope} in the window"
    if signal == "slow_queries":
        threshold = float(opts.get("threshold_seconds", 30))
        slow = slowest(rows, min_seconds=threshold, limit=10_000)
        return len(slow), f"{len(slow)} queries over {threshold:.0f}s on {scope}"
    if signal == "max_query_seconds":
        durations = [d for d in (_duration_seconds(r) for r in rows) if d is not None]
        worst = round(max(durations), 2) if durations else 0.0
        return worst, f"slowest query on {scope} took {worst}s"

    raise ConfigError(
        f"Unknown ops signal {signal!r}.",
        supported=["failed_queries", "slow_queries", "max_query_seconds",
                   "query_volume", "alert_errors", "alert_stale"],
    )
