"""Watch definitions, evaluation, and transition-based reporting.

A watch reports when its status *changes* (ok -> alert, alert -> ok, either ->
error), not on every run. A condition that stays true for a day therefore
produces one message, not forty-eight. ``renotify_after`` opts a watch back into
periodic reminders while it is still firing.
"""

from __future__ import annotations

import json
import operator
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from tathya.audit import actor
from tathya.config import Config, state_dir, watches_dir
from tathya.context import Context
from tathya.errors import ConfigError, TathyaError, UsageError
from tathya.notify import deliver

STATUS_OK = "ok"
STATUS_ALERT = "alert"
STATUS_ERROR = "error"

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str | int | float) -> int:
    """Accept ``30m``, ``2h``, ``90s``, ``1d``, or a bare number of seconds."""
    if isinstance(text, (int, float)):
        return int(text)
    match = _DURATION_RE.match(str(text))
    if not match:
        raise ConfigError(
            f"Unparseable duration {text!r}; use forms like 90s, 30m, 2h, 1d."
        )
    return int(float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()])


@dataclass(frozen=True)
class Watch:
    name: str
    path: Path
    source: dict
    condition: dict
    description: str = ""
    enabled: bool = True
    schedule: dict = field(default_factory=dict)
    notify: dict = field(default_factory=dict)
    link: str = ""

    @property
    def interval_seconds(self) -> int | None:
        every = self.schedule.get("every")
        return parse_duration(every) if every else None

    @property
    def renotify_seconds(self) -> int | None:
        value = self.notify.get("renotify_after")
        return parse_duration(value) if value else None

    def targets(self, config: Config) -> tuple[str, ...]:
        explicit = self.notify.get("targets")
        if explicit:
            return tuple(str(t) for t in explicit)
        return tuple(config.reporting.default_targets)


def _validate(raw: dict, path: Path) -> Watch:
    name = raw.get("name") or path.stem
    source = raw.get("source") or {}
    condition = raw.get("condition") or {}
    if not isinstance(source, dict) or "type" not in source:
        raise ConfigError(f"Watch {name!r} needs a source with a `type`.", file=str(path))
    if source["type"] not in {"sql", "chart", "ops"}:
        raise ConfigError(
            f"Watch {name!r} has unsupported source type {source['type']!r}; "
            "use sql, chart, or ops.",
            file=str(path),
        )
    if source["type"] == "sql":
        for key in ("database", "sql"):
            if not source.get(key):
                raise ConfigError(f"Watch {name!r} sql source needs `{key}`.", file=str(path))
    elif source["type"] == "ops":
        if not source.get("signal"):
            raise ConfigError(
                f"Watch {name!r} ops source needs a `signal`.",
                file=str(path),
                supported=["failed_queries", "slow_queries", "max_query_seconds",
                           "query_volume", "alert_errors", "alert_stale"],
            )
        if condition.get("type") == "rowcount":
            raise ConfigError(
                f"Watch {name!r} uses an ops signal, which yields a number rather than "
                "rows; use a threshold, change, or delta_pct condition.",
                file=str(path),
            )
    elif not source.get("chart_id"):
        raise ConfigError(f"Watch {name!r} chart source needs `chart_id`.", file=str(path))

    ctype = condition.get("type")
    if ctype not in {"threshold", "rowcount", "change", "delta_pct"}:
        raise ConfigError(
            f"Watch {name!r} has unsupported condition type {ctype!r}.",
            file=str(path),
            supported=["threshold", "rowcount", "change", "delta_pct"],
        )
    if ctype in {"threshold", "rowcount", "delta_pct"}:
        if condition.get("op") not in OPERATORS:
            raise ConfigError(
                f"Watch {name!r} condition needs an `op` from {sorted(OPERATORS)}.",
                file=str(path),
            )
        if condition.get("value") is None:
            raise ConfigError(f"Watch {name!r} condition needs a `value`.", file=str(path))

    return Watch(
        name=str(name),
        path=path,
        source=source,
        condition=condition,
        description=str(raw.get("description", "")),
        enabled=bool(raw.get("enabled", True)),
        schedule=raw.get("schedule") or {},
        notify=raw.get("notify") or {},
        link=str(raw.get("link", "")),
    )


def load_all(directory: Path | None = None) -> list[Watch]:
    folder = directory or watches_dir()
    if not folder.exists():
        return []
    watches: list[Watch] = []
    for path in sorted(list(folder.glob("*.yaml")) + list(folder.glob("*.yml"))):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Watch file {path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Watch file {path} must contain a mapping.")
        watches.append(_validate(raw, path))
    return watches


def load_one(name: str, directory: Path | None = None) -> Watch:
    for watch in load_all(directory):
        if watch.name == name:
            return watch
    raise UsageError(
        f"No watch named {name!r}.",
        known=[w.name for w in load_all(directory)],
        directory=str(directory or watches_dir()),
    )


class WatchState:
    """Last known status per watch, so transitions can be detected."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (state_dir() / "watch-state.json")

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {}

    def get(self, name: str) -> dict:
        return self._read().get(name, {})

    def all(self) -> dict:
        return self._read()

    def update(self, name: str, **fields: Any) -> dict:
        data = self._read()
        entry = data.setdefault(name, {})
        entry.update(fields)
        self.path.write_text(json.dumps(data, indent=2, default=str))
        self.path.chmod(0o600)
        return entry


# --------------------------------------------------------------------- evaluate
def _extract_value(rows: list[dict], column: str | None) -> Any:
    if not rows:
        return None
    first = rows[0]
    if column:
        if column not in first:
            raise ConfigError(
                f"Column {column!r} is not in the result.",
                available=list(first.keys()),
            )
        return first[column]
    return next(iter(first.values()), None)


def _coerce_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Evaluation:
    status: str
    summary: str
    value: Any = None
    rows: int = 0
    detail: dict = field(default_factory=dict)


def evaluate(ctx: Context, watch: Watch, previous: dict) -> Evaluation:
    from tathya import query as query_mod  # local import keeps the cycle out of module load

    source = watch.source

    if source["type"] == "ops":
        # Monitors production without querying production: the signal comes from
        # Superset's own metadata, so this costs no client-database load and is
        # exempt from the query rate limiter.
        from tathya import ops as ops_mod

        database_id = None
        if source.get("database"):
            database_id = ops_mod.resolve_database_id_by_name(ctx, source["database"])
        value, summary = ops_mod.ops_signal(
            ctx,
            str(source["signal"]),
            window_seconds=ops_mod.parse_window(source.get("since", "1h")),
            database=source.get("database"),
            database_id=database_id,
            threshold_seconds=source.get("threshold_seconds", 30),
        )
        detail = {
            "signal": source["signal"],
            "database": source.get("database") or "all databases",
            "window": source.get("since", "1h"),
            "cost": "superset metadata only",
        }
        ctype = watch.condition["type"]
        if ctype == "threshold":
            fired = OPERATORS[watch.condition["op"]](value, watch.condition["value"])
            summary = f"{summary} (alert when {watch.condition['op']} {watch.condition['value']})"
        elif ctype == "change":
            last = previous.get("value")
            fired = last is not None and value != last
        else:  # delta_pct
            last = _coerce_number(previous.get("value"))
            if last in (None, 0):
                fired = False
            else:
                delta = abs(value - last) / abs(last) * 100.0
                detail["delta_pct"] = round(delta, 2)
                fired = OPERATORS[watch.condition["op"]](delta, watch.condition["value"])
        return Evaluation(
            STATUS_ALERT if fired else STATUS_OK, summary, value=value, rows=0, detail=detail
        )

    if source["type"] == "sql":
        result = query_mod.run_sql(
            ctx,
            database_ref=source["database"],
            sql=source["sql"],
            schema=source.get("schema"),
            dialect=source.get("dialect"),
            command_label=f"watch.{watch.name}",
        )
        label = source["database"]
    else:
        result = query_mod.chart_data(
            ctx, int(source["chart_id"]), command_label=f"watch.{watch.name}"
        )
        label = f"chart:{source['chart_id']}"

    rows = result["rows"]
    condition = watch.condition
    ctype = condition["type"]
    detail = {
        "database": label,
        "row_count": result["row_count"],
        "duration_ms": result.get("duration_ms"),
    }

    if ctype == "rowcount":
        observed: Any = result["row_count"]
        threshold = condition["value"]
        fired = OPERATORS[condition["op"]](observed, threshold)
        summary = f"{observed} row(s) (alert when rows {condition['op']} {threshold})"
    elif ctype == "threshold":
        observed = _extract_value(rows, condition.get("column"))
        number = _coerce_number(observed)
        if number is None:
            return Evaluation(
                STATUS_ERROR,
                f"Value {observed!r} is not numeric, so the threshold cannot be applied.",
                value=observed,
                rows=result["row_count"],
                detail=detail,
            )
        threshold = condition["value"]
        fired = OPERATORS[condition["op"]](number, threshold)
        column = condition.get("column") or "value"
        summary = f"{column} = {observed} (alert when {condition['op']} {threshold})"
    elif ctype == "change":
        observed = _extract_value(rows, condition.get("column"))
        last = previous.get("value")
        fired = last is not None and observed != last
        summary = (
            f"value = {observed}" if not fired else f"value changed {last!r} -> {observed!r}"
        )
    else:  # delta_pct
        observed = _extract_value(rows, condition.get("column"))
        number = _coerce_number(observed)
        last = _coerce_number(previous.get("value"))
        if number is None:
            return Evaluation(
                STATUS_ERROR,
                f"Value {observed!r} is not numeric, so a delta cannot be computed.",
                value=observed,
                rows=result["row_count"],
                detail=detail,
            )
        if last is None or last == 0:
            fired = False
            summary = f"value = {observed} (no usable baseline yet)"
        else:
            delta = abs(number - last) / abs(last) * 100.0
            fired = OPERATORS[condition["op"]](delta, condition["value"])
            detail["delta_pct"] = round(delta, 2)
            summary = (
                f"value = {observed}, {round(delta, 1)}% vs {last} "
                f"(alert when delta {condition['op']} {condition['value']}%)"
            )

    return Evaluation(
        STATUS_ALERT if fired else STATUS_OK,
        summary,
        value=observed,
        rows=result["row_count"],
        detail=detail,
    )


def should_notify(watch: Watch, evaluation: Evaluation, previous: dict) -> tuple[bool, str]:
    last_status = previous.get("status")
    if last_status is None:
        # First observation: announce a problem, stay quiet about a healthy baseline.
        return (evaluation.status != STATUS_OK, "first_observation")
    if evaluation.status != last_status:
        return True, "status_change"
    if evaluation.status == STATUS_ALERT:
        renotify = watch.renotify_seconds
        last_notified = previous.get("last_notified_at")
        if renotify:
            # `is not None`, not truthiness: a missing timestamp means this alert
            # has never been reported, which is a reason to report it, not to
            # stay silent - and a 0 timestamp must not read as "just notified".
            if last_notified is None:
                return True, "renotify_interval"
            if (time.time() - float(last_notified)) >= renotify:
                return True, "renotify_interval"
    return False, "unchanged"


def is_due(watch: Watch, previous: dict, *, now: float | None = None) -> bool:
    interval = watch.interval_seconds
    if interval is None:
        return True
    last_run = previous.get("last_run_at")
    if last_run is None:
        return True
    return (now or time.time()) - float(last_run) >= interval


def run_watch(
    ctx: Context,
    watch: Watch,
    *,
    dry_run: bool = False,
    notify: bool = True,
    state: WatchState | None = None,
) -> dict:
    store = state or WatchState()
    previous = store.get(watch.name)
    now = time.time()

    try:
        evaluation = evaluate(ctx, watch, previous)
    except TathyaError as exc:
        evaluation = Evaluation(
            STATUS_ERROR,
            f"{exc.kind}: {exc.message}",
            detail={"error_kind": exc.kind, **(exc.details or {})},
        )

    notify_now, reason = should_notify(watch, evaluation, previous)
    event = {
        "watch": watch.name,
        "description": watch.description,
        "status": evaluation.status,
        "previous_status": previous.get("status"),
        "summary": evaluation.summary,
        "value": evaluation.value,
        "rows": evaluation.rows,
        "database": evaluation.detail.get("database"),
        "link": watch.link or ctx.config.instance.base_url,
        "actor": actor(),
        "reason": reason,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "detail": evaluation.detail,
    }

    deliveries: list[dict] = []
    if notify and notify_now:
        deliveries = deliver(event, watch.targets(ctx.config), ctx.config, dry_run=dry_run)

    if not dry_run:
        fields: dict[str, Any] = {
            "status": evaluation.status,
            "value": evaluation.value,
            "rows": evaluation.rows,
            "summary": evaluation.summary,
            "last_run_at": now,
        }
        if notify_now:
            fields["last_notified_at"] = now
            fields["last_notified_status"] = evaluation.status
        store.update(watch.name, **fields)

    ctx.audit.record(
        command=f"watch.run:{watch.name}",
        outcome=evaluation.status,
        notified=notify_now,
        notify_reason=reason,
        rows=evaluation.rows,
        dry_run=dry_run,
    )

    return {
        "watch": watch.name,
        "status": evaluation.status,
        "previous_status": previous.get("status"),
        "summary": evaluation.summary,
        "value": evaluation.value,
        "rows": evaluation.rows,
        "notified": notify_now,
        "notify_reason": reason,
        "deliveries": deliveries,
        "detail": evaluation.detail,
        "dry_run": dry_run,
    }
