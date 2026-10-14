"""Watch commands: list, show, run, and inspect state."""

from __future__ import annotations

from tathya import watch as watch_mod
from tathya.context import Context
from tathya.errors import EXIT_ALERT, EXIT_OK
from tathya.output import render_table


def cmd_watch_list(ctx: Context) -> int:
    state = watch_mod.WatchState()
    rows = []
    for item in watch_mod.load_all():
        previous = state.get(item.name)
        rows.append(
            {
                "name": item.name,
                "enabled": item.enabled,
                "source": item.source.get("type"),
                "target": (item.source.get("database") or item.source.get("chart_id")
                           or item.source.get("signal")),
                "condition": item.condition.get("type"),
                "every": item.schedule.get("every"),
                "targets": ",".join(item.targets(ctx.config)),
                "last_status": previous.get("status"),
                "due": watch_mod.is_due(item, previous),
            }
        )

    def human(data, line):
        render_table(
            data,
            ["name", "enabled", "source", "target", "condition", "every", "targets", "last_status", "due"],
            line,
        )

    return ctx.emitter.ok("watch.list", rows, human=human)


def cmd_watch_show(ctx: Context) -> int:
    item = watch_mod.load_one(ctx.args.name)
    state = watch_mod.WatchState().get(item.name)
    return ctx.emitter.ok(
        "watch.show",
        {
            "name": item.name,
            "description": item.description,
            "enabled": item.enabled,
            "file": str(item.path),
            "source": item.source,
            "condition": item.condition,
            "schedule": item.schedule,
            "notify": item.notify,
            "resolved_targets": list(item.targets(ctx.config)),
            "state": state,
        },
    )


def cmd_watch_state(ctx: Context) -> int:
    return ctx.emitter.ok("watch.state", watch_mod.WatchState().all())


def cmd_watch_run(ctx: Context) -> int:
    if ctx.args.all:
        watches = watch_mod.load_all()
    else:
        watches = [watch_mod.load_one(name) for name in ctx.args.names]

    state = watch_mod.WatchState()
    results, skipped = [], []
    for item in watches:
        if not item.enabled and not ctx.args.include_disabled:
            skipped.append({"watch": item.name, "reason": "disabled"})
            continue
        if ctx.args.due_only and not watch_mod.is_due(item, state.get(item.name)):
            skipped.append({"watch": item.name, "reason": "not_due"})
            continue
        results.append(
            watch_mod.run_watch(
                ctx,
                item,
                dry_run=ctx.args.dry_run,
                notify=not ctx.args.no_notify,
                state=state,
            )
        )

    alerting = [r for r in results if r["status"] not in (watch_mod.STATUS_OK, watch_mod.STATUS_DEFERRED)]

    def human(data, line):
        render_table(
            data["results"],
            ["watch", "status", "previous_status", "summary", "notified", "notify_reason"],
            line,
        )
        if data["skipped"]:
            line("")
            line("skipped: " + ", ".join(f"{s['watch']} ({s['reason']})" for s in data["skipped"]))

    payload = {
        "results": results,
        "skipped": skipped,
        "alerting": [r["watch"] for r in alerting],
        "dry_run": ctx.args.dry_run,
    }
    # Exit 9 when anything is not OK, so a scheduler or agent can branch on it.
    exit_code = EXIT_ALERT if (alerting and not ctx.args.no_alert_exit) else EXIT_OK
    return ctx.emitter.ok("watch.run", payload, human=human, exit_code=exit_code)
