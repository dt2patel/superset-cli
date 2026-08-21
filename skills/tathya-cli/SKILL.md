---
name: tathya-cli
description: Query and manage the Tathya (Apache Superset) instance through the guarded `tathya` CLI, and set up watches that report to Google Chat when something changes. Use when a request mentions Tathya, Superset, a Tathya dashboard/chart/dataset, ad-hoc OMS reporting numbers, BI query results, or alerting on an OMS metric.
---

# Tathya CLI

Use the `tathya` command as the only path to Tathya. Do not call the Superset
REST API directly with `curl`, and do not connect to a client OMS database by
any other route — the CLI is where the read-only guard, the database allowlist,
the rate limits, and the audit log live.

The Superset native MCP server needs 5.0+; Tathya runs 4.0.0, so it is not an
option. `tathya` is the substitute.

## Why the caps are real

Tathya connects **directly to per-client production OMS read-replicas**. On
Heavy queries through this deployment have driven a production replica to 100%
CPU and triggered infrastructure alarms. Treat every query as landing on live client
infrastructure.

## Cold install

If `tathya health` reports a config error or a missing password, the user has not
set up yet. Tell them to run **`tathya setup`** — an interactive wizard that
takes a base URL and credentials and derives everything else from the instance
(URL normalisation, provider guidance, per-database `dialect`, pinned ids,
starter watches). It is deliberately interactive and cannot be scripted, because
the password prompt must reach a human. Do not attempt to hand-write the config
for them unless they ask.

## Always

- Set `TATHYA_ACTOR` to identify yourself, e.g.
  `TATHYA_ACTOR=agent:fulfillment-review tathya sql run ...`. It is recorded in
  the audit log and is how a human later attributes load to an agent.
- Pipe/parse the JSON envelope. Output is JSON automatically when stdout is not
  a terminal; add `--json` if unsure. Shape:
  `{"ok": true, "command": ..., "data": ..., "warnings": [...]}` or
  `{"ok": false, "error": {"kind","message","retryable","details"}, "exit_code": N}`.
- Prefer an existing chart (`tathya chart data <id>`) over ad-hoc SQL when it
  already answers the question — the query is one Superset already runs.
- Run `tathya sql run <db> "<sql>" --explain-only` first for anything wide or
  unfamiliar. It shows the guard's rewritten SQL and its warnings **without
  contacting the database**.

## Never

- **Never retry on exit 5 (guard refused) or exit 6 (rate limited / blackout).**
  These are terminal by design. On 5, rewrite the query. On 6, stop and report
  that the budget is exhausted — looping is the exact failure this tool exists
  to prevent.
- Never widen a row cap. `--max-rows` can only tighten; asking for more silently
  yields the configured cap.
- Never add a database to the allowlist to get a query to run. Report that it is
  not allowlisted and let a human decide.
- Never ask a human to paste a password or webhook into chat. Direct them to
  `tathya auth login` / `tathya notify set-webhook`, which prompt on the
  terminal and store the value in the Keychain.
- Never send a Google Chat message as a side effect. `notify test` and
  `watch run --dry-run` preview; posting needs explicit `--confirm`.

## Reading

```bash
tathya health                        # connectivity, identity, asset counts
tathya db list                       # shows which databases are agent-allowlisted
tathya dataset list --search order
tathya dataset get <id>              # columns, types, metrics
tathya dashboard list
tathya dashboard get <id-or-slug>
tathya dashboard charts <id-or-slug>
tathya dashboard datasets <id-or-slug>
tathya chart list --search stuck
tathya chart get <id>
tathya chart data <id>               # executes the saved chart's query
```

## Ad-hoc answers

```bash
tathya sql run <database> "SELECT ..."      # single read-only statement
tathya sql run <database> --file query.sql
tathya sql run <database> --stdin
tathya sql run <database> "..." --explain-only    # guard decision, no DB contact
tathya sql estimate <database> --file query.sql   # engine cost, where supported
tathya sql run <database> "..." --format csv
```

The guard accepts one `SELECT`/`WITH` statement. It rejects stacked statements,
any write or DDL node anywhere in the tree (including Postgres
`WITH … (DELETE … RETURNING) SELECT`), `SELECT … INTO`, `FOR UPDATE`/`FOR SHARE`,
session-stalling functions, and anything it cannot parse. Write narrow, indexed,
`WHERE`-filtered queries; heed the warnings about `SELECT *`, missing `WHERE`,
and conditionless joins.

## Managing dashboards and charts

Writes are a **dry run** unless `--confirm` is passed *and*
`TATHYA_ALLOW_WRITE=1` is set. Run without `--confirm` first, show the planned
calls, and only then apply.

```bash
tathya chart create --name "..." --dataset-id <id> --viz-type table \
    [--params-file params.json] [--dashboard-ids 1 2]
tathya chart update <id> --name "..." [--params-file params.json]
tathya dashboard create --title "..." [--slug ...] [--published]
tathya dashboard update <id> --title "..." [--position-json-file layout.json]
tathya dashboard add-chart <dashboard-id> <chart-id> [--width 4] [--height 50]
tathya export dashboard <id> --out asset.zip
```

Deleting also requires `safety.allow_delete = true` in config. If a delete is
blocked, report it rather than editing the config.

`add-chart` appends the chart in a new full-width row and links it to the
dashboard (two API calls). Finer layout needs `--position-json-file`.

## Monitoring production — prefer this over querying it

Superset records its own query history and the state of its 250+ Alerts &
Reports. These commands read that metadata, so they cost **no client-database
load**, do **not** consume the query budget, and cover **all** databases rather
than only allowlisted ones.

**When asked how production is doing, reach for these before writing any SQL.**

```bash
tathya digest                            # whole picture; exits 9 if attention needed
tathya digest --since 24h
tathya ops load --since 6h               # volume + avg/p95/max per database
tathya ops slow --since 24h --min-seconds 30
tathya ops failures --since 6h
tathya ops queries --database "Northwind OMS" --status failed
tathya ops alerts                        # which Superset alerts are broken or overdue
```

`ops alerts` separates four states worth acting on: **error** (Superset's own
verdict), **stale** (missed its scheduled run, judged against its crontab in its
own timezone), **never_run**, and **inactive**. Staleness is schedule-relative,
so do not report a monthly report as stale merely because it has been idle for
weeks — the command already accounts for that.

When investigating a slow or failing database, `ops` tells you who ran what and
how long it took without you running anything against the database yourself.

## Watches — reporting when something happens

A watch is a query plus a condition, evaluated on a schedule, reporting only on
**status transitions** (ok→alert, alert→ok, →error). Definitions live in
`~/.config/tathya/watches/*.yaml`.

```bash
tathya watch list                       # definitions plus last known status
tathya watch show <name>
tathya watch run --all --due-only       # what the scheduler runs
tathya watch run <name> --dry-run       # preview message, no send, no state change
tathya watch state
```

`watch run` exits **9** when anything is alerting. That is a signal, not a
failure — read `data.alerting`.

Condition types: `threshold` (numeric column vs value), `rowcount`,
`change` (value differs from last run), `delta_pct` (percent move vs last run).
Operators: `> >= < <= == !=`.

Sources: `sql` (guarded ad-hoc query), `chart` (a saved chart), or **`ops`** (a
monitoring signal from Superset metadata — `failed_queries`, `slow_queries`,
`max_query_seconds`, `query_volume`, `alert_errors`, `alert_stale`).

**Prefer an `ops` source when the question is about production health.** It
costs no client-database load, so it can run every 15 minutes without adding
pressure to a replica. Reserve `sql` sources for business conditions that only a
query can answer. `rowcount` conditions do not apply to `ops` sources.

When asked to create a watch, write the YAML, then verify it with
`tathya watch run <name> --dry-run` before telling the user it is live.

## Accountability

```bash
tathya limits                            # remaining query budget, blackout state
tathya audit tail --limit 50
tathya audit tail --outcome guard_rejected
tathya audit tail --actor-filter agent:fulfillment-review
```

## When something is blocked

Report the exit code, the `error.kind`, and what a human would need to change
(allowlist entry, `allow_delete`, blackout window, a narrower query). Do not
work around a control. If deployment facts are needed — Tathya's version, config
location, restart access — loop in whoever owns the Superset deployment and its
infrastructure; the person running this CLI generally does not.
