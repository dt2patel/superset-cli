"""Command-line entry point.

Command names are stable and the JSON envelope plus exit codes form the contract
agents rely on, so both are treated as API surface rather than presentation.
"""

from __future__ import annotations

import argparse
import os
import sys

from tathya import __version__
from tathya.commands import digestcmd, meta, opscmd, read, setup, sqlcmd, watchcmd, writecmd
from tathya.config import load as load_config
from tathya.context import Context
from tathya.errors import EXIT_USAGE, TathyaError, UsageError
from tathya.output import Emitter


def _add_listing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search", help="case-insensitive substring filter on the name column")
    parser.add_argument("--limit", type=int, default=25, help="rows per page (max 100)")
    parser.add_argument("--all", action="store_true", help="page through every result")


def _add_confirm(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="actually perform the write (also needs TATHYA_ALLOW_WRITE=1); "
        "without it the command is a dry run",
    )


def _add_sql_source(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("database", help="allowlisted database name or id")
    parser.add_argument("sql", nargs="?", help="the SELECT statement")
    parser.add_argument("--file", help="read SQL from a file instead")
    parser.add_argument("--stdin", action="store_true", help="read SQL from stdin instead")
    parser.add_argument("--schema", help="schema to run against")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tathya",
        description="Safe CLI bridge between AI agents and the Tathya (Superset) instance.",
    )
    parser.add_argument("--version", action="version", version=f"tathya-cli {__version__}")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", dest="output_mode", action="store_const", const="json",
                        help="force the machine-readable envelope")
    output.add_argument("--text", dest="output_mode", action="store_const", const="text",
                        help="force human-readable output")
    parser.set_defaults(output_mode="auto")
    parser.add_argument("--config", help="path to config.toml (default ~/.config/tathya/config.toml)")
    parser.add_argument("--actor", help="who is running this; recorded in the audit log")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ---------------------------------------------------------------- setup
    p_setup = sub.add_parser(
        "setup", help="interactive first-run configuration (recommended)"
    )
    p_setup.add_argument("--force", action="store_true",
                         help="overwrite an existing config without asking")
    p_setup.set_defaults(func=setup.cmd_setup, needs_config=False)

    p_init = sub.add_parser("init", help="write a starter config file to edit by hand")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")
    p_init.set_defaults(func=meta.cmd_init, needs_config=False)

    p_auth = sub.add_parser("auth", help="authentication").add_subparsers(dest="sub", metavar="<action>")
    p_login = p_auth.add_parser("login", help="store the password in the Keychain and log in")
    p_login.add_argument("--reuse-stored", action="store_true",
                         help="use the already-stored Keychain password instead of prompting")
    p_login.set_defaults(func=meta.cmd_auth_login)
    p_status = p_auth.add_parser("status", help="show token and Keychain state")
    p_status.add_argument("--probe", action="store_true", help="also call /api/v1/me/ to verify")
    p_status.set_defaults(func=meta.cmd_auth_status)
    p_logout = p_auth.add_parser("logout", help="clear the cached session")
    p_logout.add_argument("--forget-password", action="store_true",
                          help="also remove the password from the Keychain")
    p_logout.set_defaults(func=meta.cmd_auth_logout)

    sub.add_parser("health", help="check connectivity, identity, and visible asset counts").set_defaults(
        func=meta.cmd_health
    )
    sub.add_parser("limits", help="show remaining query budget and blackout state").set_defaults(
        func=meta.cmd_limits
    )

    # ------------------------------------------------------------- metadata
    p_db = sub.add_parser("db", help="databases").add_subparsers(dest="sub", metavar="<action>")
    p_db_list = p_db.add_parser("list", help="list databases and whether agents may query them")
    _add_listing_args(p_db_list)
    p_db_list.set_defaults(func=read.cmd_db_list)

    p_ds = sub.add_parser("dataset", help="datasets").add_subparsers(dest="sub", metavar="<action>")
    p_ds_list = p_ds.add_parser("list", help="list datasets")
    _add_listing_args(p_ds_list)
    p_ds_list.set_defaults(func=read.cmd_dataset_list)
    p_ds_get = p_ds.add_parser("get", help="show a dataset's columns and metrics")
    p_ds_get.add_argument("dataset_id", type=int)
    p_ds_get.add_argument("--raw", action="store_true", help="include the full API payload")
    p_ds_get.set_defaults(func=read.cmd_dataset_get)

    p_chart = sub.add_parser("chart", help="charts").add_subparsers(dest="sub", metavar="<action>")
    p_chart_list = p_chart.add_parser("list", help="list charts")
    _add_listing_args(p_chart_list)
    p_chart_list.set_defaults(func=read.cmd_chart_list)
    p_chart_get = p_chart.add_parser("get", help="show a chart definition")
    p_chart_get.add_argument("chart_id", type=int)
    p_chart_get.set_defaults(func=read.cmd_chart_get)
    p_chart_data = p_chart.add_parser("data", help="run a saved chart's query and return its data")
    p_chart_data.add_argument("chart_id", type=int)
    p_chart_data.set_defaults(func=read.cmd_chart_data)

    p_chart_create = p_chart.add_parser("create", help="create a chart")
    p_chart_create.add_argument("--name", required=True)
    p_chart_create.add_argument("--dataset-id", type=int, required=True)
    p_chart_create.add_argument("--viz-type", required=True, help="e.g. table, big_number_total, echarts_timeseries_line")
    p_chart_create.add_argument("--params-file", help="JSON file of viz params")
    p_chart_create.add_argument("--description")
    p_chart_create.add_argument("--dashboard-ids", nargs="*", help="dashboards to attach it to")
    _add_confirm(p_chart_create)
    p_chart_create.set_defaults(func=writecmd.cmd_chart_create)

    p_chart_update = p_chart.add_parser("update", help="update a chart")
    p_chart_update.add_argument("chart_id", type=int)
    p_chart_update.add_argument("--name")
    p_chart_update.add_argument("--viz-type")
    p_chart_update.add_argument("--description")
    p_chart_update.add_argument("--params-file")
    p_chart_update.add_argument("--dashboard-ids", nargs="*")
    _add_confirm(p_chart_update)
    p_chart_update.set_defaults(func=writecmd.cmd_chart_update)

    p_chart_delete = p_chart.add_parser("delete", help="delete a chart (needs safety.allow_delete)")
    p_chart_delete.add_argument("chart_id", type=int)
    _add_confirm(p_chart_delete)
    p_chart_delete.set_defaults(func=writecmd.cmd_chart_delete)

    p_dash = sub.add_parser("dashboard", help="dashboards").add_subparsers(dest="sub", metavar="<action>")
    p_dash_list = p_dash.add_parser("list", help="list dashboards")
    _add_listing_args(p_dash_list)
    p_dash_list.set_defaults(func=read.cmd_dashboard_list)
    for name, handler, helptext in (
        ("get", read.cmd_dashboard_get, "show a dashboard definition"),
        ("charts", read.cmd_dashboard_charts, "list the charts on a dashboard"),
        ("datasets", read.cmd_dashboard_datasets, "list the datasets a dashboard uses"),
    ):
        item = p_dash.add_parser(name, help=helptext)
        item.add_argument("dashboard", help="dashboard id or slug")
        item.set_defaults(func=handler)

    p_dash_create = p_dash.add_parser("create", help="create a dashboard")
    p_dash_create.add_argument("--title", required=True)
    p_dash_create.add_argument("--slug")
    p_dash_create.add_argument("--published", action="store_true")
    p_dash_create.add_argument("--json-metadata-file")
    p_dash_create.add_argument("--position-json-file")
    _add_confirm(p_dash_create)
    p_dash_create.set_defaults(func=writecmd.cmd_dashboard_create)

    p_dash_update = p_dash.add_parser("update", help="update a dashboard")
    p_dash_update.add_argument("dashboard_id", type=int)
    p_dash_update.add_argument("--title")
    p_dash_update.add_argument("--slug")
    p_dash_update.add_argument("--published", type=lambda v: v.lower() in {"1", "true", "yes"},
                               default=None, metavar="true|false")
    p_dash_update.add_argument("--json-metadata-file")
    p_dash_update.add_argument("--position-json-file")
    _add_confirm(p_dash_update)
    p_dash_update.set_defaults(func=writecmd.cmd_dashboard_update)

    p_dash_delete = p_dash.add_parser("delete", help="delete a dashboard (needs safety.allow_delete)")
    p_dash_delete.add_argument("dashboard_id", type=int)
    _add_confirm(p_dash_delete)
    p_dash_delete.set_defaults(func=writecmd.cmd_dashboard_delete)

    p_add_chart = p_dash.add_parser("add-chart", help="append an existing chart to a dashboard")
    p_add_chart.add_argument("dashboard_id", type=int)
    p_add_chart.add_argument("chart_id", type=int)
    p_add_chart.add_argument("--width", type=int, default=4, help="grid columns (1-12)")
    p_add_chart.add_argument("--height", type=int, default=50)
    p_add_chart.add_argument("--allow-duplicate", action="store_true")
    _add_confirm(p_add_chart)
    p_add_chart.set_defaults(func=writecmd.cmd_dashboard_add_chart)

    p_export = sub.add_parser("export", help="export assets as a ZIP of YAML")
    p_export.add_argument("kind", choices=["dashboard", "chart", "dataset"])
    p_export.add_argument("ids", nargs="+")
    p_export.add_argument("--out", required=True, help="destination .zip path")
    p_export.add_argument("--force", action="store_true")
    p_export.set_defaults(func=read.cmd_export)

    # ------------------------------------------------------------------ sql
    p_sql = sub.add_parser("sql", help="guarded ad-hoc SQL").add_subparsers(dest="sub", metavar="<action>")
    p_sql_run = p_sql.add_parser("run", help="run a read-only SELECT against an allowlisted database")
    _add_sql_source(p_sql_run)
    p_sql_run.add_argument("--dialect", help="sqlglot dialect for parsing, e.g. postgres, mysql")
    p_sql_run.add_argument("--max-rows", type=int, help="tighten the row cap (cannot raise it)")
    p_sql_run.add_argument("--format", choices=["auto", "csv"], default="auto")
    p_sql_run.add_argument("--estimate-first", action="store_true", help="cost the query before running it")
    p_sql_run.add_argument("--estimate-only", action="store_true", help="with --estimate-first, stop after costing")
    p_sql_run.add_argument("--explain-only", action="store_true",
                           help="show the guard's decision and rewritten SQL without touching the database")
    p_sql_run.set_defaults(func=sqlcmd.cmd_sql_run)

    p_sql_est = p_sql.add_parser("estimate", help="ask the engine to cost a query without running it")
    _add_sql_source(p_sql_est)
    p_sql_est.set_defaults(func=sqlcmd.cmd_sql_estimate)

    # ------------------------------------------------------------------- ops
    p_ops = sub.add_parser(
        "ops", help="production monitoring from Superset's own metadata (no client DB load)"
    ).add_subparsers(dest="sub", metavar="<action>")

    def _ops_common(parser, *, database=True):
        parser.add_argument("--since", help="window, e.g. 30m, 6h, 2d (default from config)")
        if database:
            parser.add_argument("--database", help="restrict to one database (name or id)")

    p_ops_load = p_ops.add_parser("load", help="query volume and duration per database")
    _ops_common(p_ops_load)
    p_ops_load.add_argument("--top", type=int, help="how many databases to show")
    p_ops_load.set_defaults(func=opscmd.cmd_ops_load)

    p_ops_slow = p_ops.add_parser("slow", help="slowest queries in the window")
    _ops_common(p_ops_slow)
    p_ops_slow.add_argument("--min-seconds", type=float, help="threshold (default from config)")
    p_ops_slow.add_argument("--limit", type=int, default=15)
    p_ops_slow.set_defaults(func=opscmd.cmd_ops_slow)

    p_ops_fail = p_ops.add_parser("failures", help="failed queries, grouped")
    _ops_common(p_ops_fail)
    p_ops_fail.add_argument("--limit", type=int, default=20)
    p_ops_fail.set_defaults(func=opscmd.cmd_ops_failures)

    p_ops_q = p_ops.add_parser("queries", help="recent query history")
    _ops_common(p_ops_q)
    p_ops_q.add_argument("--status", help="e.g. success, failed, running")
    p_ops_q.add_argument("--sql-contains", help="substring match on the SQL")
    p_ops_q.add_argument("--limit", type=int, default=25)
    p_ops_q.set_defaults(func=opscmd.cmd_ops_queries)

    p_ops_alerts = p_ops.add_parser("alerts", help="health of Superset's own alerts & reports")
    p_ops_alerts.set_defaults(func=opscmd.cmd_ops_alerts)

    # ---------------------------------------------------------------- digest
    p_digest = sub.add_parser("digest", help="the whole production picture in one command")
    p_digest.add_argument("--since", help="window, e.g. 30m, 6h, 2d (default from config)")
    p_digest.add_argument("--target", action="append", choices=["local", "gchat"],
                          help="also report to this channel; repeatable")
    p_digest.add_argument("--no-alert-exit", action="store_true",
                          help="always exit 0 instead of 9 when something needs attention")
    _add_confirm(p_digest)
    p_digest.set_defaults(func=digestcmd.cmd_digest)

    # ---------------------------------------------------------------- watch
    p_watch = sub.add_parser("watch", help="scheduled checks that report when things change").add_subparsers(
        dest="sub", metavar="<action>"
    )
    p_watch.add_parser("list", help="list watches and their last known status").set_defaults(
        func=watchcmd.cmd_watch_list
    )
    p_watch_show = p_watch.add_parser("show", help="show one watch definition and state")
    p_watch_show.add_argument("name")
    p_watch_show.set_defaults(func=watchcmd.cmd_watch_show)
    p_watch.add_parser("state", help="dump stored watch state").set_defaults(func=watchcmd.cmd_watch_state)

    p_watch_run = p_watch.add_parser("run", help="evaluate watches and report changes")
    p_watch_run.add_argument("names", nargs="*", help="watch names (omit with --all)")
    p_watch_run.add_argument("--all", action="store_true", help="run every watch")
    p_watch_run.add_argument("--due-only", action="store_true", help="skip watches whose interval has not elapsed")
    p_watch_run.add_argument("--no-notify", action="store_true", help="evaluate without sending anything")
    p_watch_run.add_argument("--dry-run", action="store_true",
                             help="preview notifications and leave stored state untouched")
    p_watch_run.add_argument("--include-disabled", action="store_true")
    p_watch_run.add_argument("--no-alert-exit", action="store_true",
                             help="always exit 0 instead of 9 when a watch is alerting")
    p_watch_run.set_defaults(func=watchcmd.cmd_watch_run)

    # --------------------------------------------------------------- notify
    p_notify = sub.add_parser("notify", help="reporting channels").add_subparsers(dest="sub", metavar="<action>")
    p_notify.add_parser("set-webhook", help="store the Google Chat webhook in the Keychain").set_defaults(
        func=meta.cmd_notify_set_webhook
    )
    p_notify_test = p_notify.add_parser("test", help="preview or send a test message")
    p_notify_test.add_argument("--target", action="append", choices=["local", "gchat"],
                               help="channel to use; repeatable")
    _add_confirm(p_notify_test)
    p_notify_test.set_defaults(func=meta.cmd_notify_test)

    # ---------------------------------------------------------------- audit
    p_audit = sub.add_parser("audit", help="what agents have done").add_subparsers(dest="sub", metavar="<action>")
    p_audit_tail = p_audit.add_parser("tail", help="show recent audit entries")
    p_audit_tail.add_argument("--limit", type=int, default=20)
    p_audit_tail.add_argument("--actor-filter", help="only entries from this actor")
    p_audit_tail.add_argument("--command", help="only commands starting with this prefix")
    p_audit_tail.add_argument("--outcome", help="only this outcome, e.g. success or guard_rejected")
    p_audit_tail.set_defaults(func=meta.cmd_audit_tail)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    emitter = Emitter(args.output_mode)

    if getattr(args, "func", None) is None:
        # A bare group like `tathya auth` with no action.
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    if args.actor:
        os.environ["TATHYA_ACTOR"] = args.actor

    command_name = " ".join(
        part for part in (getattr(args, "command", None), getattr(args, "sub", None)) if part
    )

    ctx: Context | None = None
    try:
        config = None
        if getattr(args, "needs_config", True):
            from pathlib import Path

            config = load_config(Path(args.config).expanduser() if args.config else None)
            # Config-level problems apply to whatever command follows.
            emitter.ambient_warnings.extend(config.warnings)
        ctx = Context(config=config, emitter=emitter, args=args)  # type: ignore[arg-type]
        return args.func(ctx)
    except TathyaError as err:
        return emitter.error(command_name, err)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    finally:
        if ctx is not None:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
