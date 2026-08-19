"""`tathya digest` - the whole production picture in one command.

Built entirely from Superset's own metadata plus local state, so running it
costs nothing on any client database and can be scheduled as often as you like.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from tathya import ops, watch as watch_mod
from tathya.context import Context
from tathya.errors import EXIT_ALERT, EXIT_OK
from tathya.notify import deliver_text


def build_digest(ctx: Context, window_seconds: int) -> dict:
    thresholds = ctx.config.digest
    rows, truncated = ops.fetch_queries(ctx, window_seconds=window_seconds)
    load = ops.summarize_load(rows)
    slow = ops.slowest(rows, min_seconds=thresholds.slow_query_seconds, limit=5)
    alert_health = ops.classify_reports(ops.fetch_reports(ctx))

    attention: list[str] = []
    flagged: list[dict] = []
    for entry in load["databases"]:
        reasons = []
        if (entry["max_seconds"] or 0) > thresholds.slow_query_seconds:
            reasons.append(f"slowest {entry['max_seconds']}s")
        if entry["failure_rate_pct"] > thresholds.failure_rate_pct and entry["queries"] >= 5:
            reasons.append(f"{entry['failure_rate_pct']}% failing")
        if reasons:
            flagged.append({**entry, "reasons": reasons})
            attention.append(f"{entry['database']}: {', '.join(reasons)}")

    if alert_health["error"]:
        attention.append(f"{len(alert_health['error'])} alerts in error state")
    if alert_health["stale"]:
        attention.append(
            f"{len(alert_health['stale'])} alerts missed their scheduled run"
        )

    watch_state = watch_mod.WatchState().all()
    alerting_watches = [
        {"watch": name, "summary": entry.get("summary")}
        for name, entry in watch_state.items()
        if entry.get("status") == watch_mod.STATUS_ALERT
    ]
    if alerting_watches:
        attention.append(f"{len(alerting_watches)} watches alerting")

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": ctx.args.since or thresholds.window,
        "window_seconds": window_seconds,
        "truncated": truncated,
        "query_pressure": {
            "total_queries": load["total_queries"],
            "failure_rate_pct": load["failure_rate_pct"],
            "statuses": load["statuses"],
            "databases": load["databases"][: thresholds.top_databases],
            "flagged": flagged,
            "top_users": load["top_users"][:5],
        },
        "slowest": slow,
        "alerts": {
            "total": alert_health["total"],
            "counts": alert_health["counts"],
            "error": alert_health["error"][:8],
            "stale": alert_health["stale"][:8],
        },
        "watches": {"total": len(watch_state), "alerting": alerting_watches},
        "agent_budget": ctx.limiter.status(),
        "attention": attention,
        "thresholds": {
            "slow_query_seconds": thresholds.slow_query_seconds,
            "failure_rate_pct": thresholds.failure_rate_pct,
        },
    }


def render_lines(digest: dict, bold: Callable[[str], str] = lambda s: s) -> list[str]:
    """One renderer for terminal and Chat; `bold` decides the markup."""
    out: list[str] = []
    out.append(bold(f"Tathya production digest · last {digest['window']}"))
    out.append(digest["generated_at"])

    attention = digest["attention"]
    out.append("")
    if attention:
        out.append(bold(f"⚠ {len(attention)} thing(s) need attention"))
        out.extend(f"  • {item}" for item in attention)
    else:
        out.append(bold("✓ Nothing needs attention"))

    pressure = digest["query_pressure"]
    out.append("")
    out.append(bold(f"Query load — {pressure['total_queries']} queries, "
                    f"{pressure['failure_rate_pct']}% failed"))
    for entry in pressure["databases"]:
        mark = " ←" if any(f["database"] == entry["database"] for f in pressure["flagged"]) else ""
        out.append(
            f"  {entry['database'][:30]:30} {entry['queries']:>4}q  "
            f"avg {str(entry['avg_seconds'] or 0):>6}s  max {str(entry['max_seconds'] or 0):>7}s  "
            f"{entry['failures']} failed{mark}"
        )

    if digest["slowest"]:
        out.append("")
        out.append(bold(f"Slowest (over {digest['thresholds']['slow_query_seconds']:g}s)"))
        for entry in digest["slowest"]:
            out.append(f"  {entry['seconds']:>7}s  {entry['database'][:24]:24} {entry['user'][:20]}")

    alerts = digest["alerts"]
    out.append("")
    out.append(bold(f"Alerts & reports — {alerts['total']} configured"))
    out.append("  " + " · ".join(f"{k} {v}" for k, v in alerts["counts"].items()))
    for entry in alerts["error"]:
        out.append(f"  ! error  {str(entry['name'])[:44]:44} {entry['schedule'] or ''}")
    for entry in alerts["stale"]:
        overdue = entry.get("overdue_by_hours")
        detail = f"overdue {overdue}h" if overdue is not None else "schedule unknown"
        out.append(f"  ! stale  {str(entry['name'])[:44]:44} {detail} "
                   f"(last ran {entry['last_eval_age_hours']}h ago)")

    watches = digest["watches"]
    out.append("")
    out.append(bold(f"Watches — {watches['total']} defined, {len(watches['alerting'])} alerting"))
    for entry in watches["alerting"]:
        out.append(f"  ! {entry['watch']}: {entry['summary']}")

    budget = digest["agent_budget"]
    used = budget.get("used_today", {}).get("global", 0)
    out.append("")
    out.append(bold("Agent query budget"))
    out.append(f"  {used}/{budget['limits']['max_queries_per_day']} used today · "
               f"{budget['limits']['max_queries_per_minute']}/min cap")
    return out


def cmd_digest(ctx: Context) -> int:
    window = ops.parse_window(ctx.args.since or ctx.config.digest.window)
    digest = build_digest(ctx, window)

    targets = tuple(ctx.args.target or ())
    deliveries: list[dict] = []
    if targets:
        text = "\n".join(render_lines(digest, bold=lambda s: f"*{s}*"))
        dry_run = not ctx.confirmed
        deliveries = deliver_text(
            text, targets, ctx.config, dry_run=dry_run,
            title="Tathya production digest", thread_key="tathya-digest",
        )
        digest["deliveries"] = deliveries

    def human(data, line):
        for text_line in render_lines(data):
            line(text_line)
        if data.get("deliveries"):
            line("")
            for delivery in data["deliveries"]:
                state = "sent" if delivery.get("delivered") else (
                    "preview only" if delivery.get("dry_run") else f"failed: {delivery.get('error')}")
                line(f"[{delivery['target']}] {state}")

    warnings = []
    if targets and not ctx.confirmed:
        warnings.append("Preview only — nothing was sent. Add --confirm to actually post.")
    exit_code = EXIT_ALERT if (digest["attention"] and not ctx.args.no_alert_exit) else EXIT_OK
    return ctx.emitter.ok("digest", digest, warnings=warnings, human=human, exit_code=exit_code)
