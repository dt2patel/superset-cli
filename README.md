# tathya

A safety-first CLI for [Apache Superset](https://superset.apache.org) — built so
that **AI agents can query and manage a Superset instance without being able to
harm the production databases behind it**, plus a monitoring layer that watches
production by reading Superset's own records rather than querying it.

Tested against **Superset 4.0.0**.

```bash
tathya digest                       # whole production picture, one command
tathya sql run analytics "SELECT status, COUNT(*) FROM orders GROUP BY 1"
tathya ops alerts                   # which of your alerts & reports are broken
```

## Why this exists

Superset is often wired straight to **live production databases** rather than an
isolated warehouse. That makes it a lovely BI tool and a dangerous thing to hand
an autonomous agent: heavy hand-run queries have pushed production read-replicas
to 100% CPU, and an agent that retries a failed query in a loop is a strictly
worse version of that.

Superset 5.0+ ships a native MCP server. On 4.x it does not exist, so this CLI
fills the gap — and it adds guardrails an MCP server does not: a deny-by-default
database allowlist, a read-only SQL guard, locally-enforced rate limits, and an
audit log.

---

# Setup

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| macOS | Secrets are stored in the macOS Keychain via `security` |
| Python 3.11+ | `tomllib` and `zoneinfo` are used from the stdlib |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` |
| A Superset account using **`db` or `ldap`** auth | Superset's JWT login endpoint has no OAuth/SSO branch, so an SSO-only account cannot obtain a token |

Check your auth type by looking at the Superset login page: a username/password
form means `db` or `ldap` and you are fine; a "Sign in with Google" button alone
means you need a `db`-provider service account instead.

## 2. Install

```bash
git clone https://github.com/dt2patel/superset-cli.git
cd superset-cli
uv tool install --editable .
```

That puts `tathya` on your `PATH` (usually `~/.local/bin/tathya`). Verify:

```bash
tathya --version
```

## 3. Run the setup wizard

```bash
tathya setup
```

Paste a base URL and credentials; everything else is discovered from the
instance. Seven steps, each verified before the next:

1. **Environment** — macOS, Python, Keychain availability
2. **Instance** — accepts any URL and reduces it to the origin (paste the page
   you were looking at and it strips the path), then proves `/health` and
   `/api/v1/` respond *before* asking for anything secret
3. **Authentication** — inspects the login page and tells you whether db/ldap
   will work or the account is SSO-only (which Superset's JWT login cannot use)
4. **Password** — prompted hidden, straight into the Keychain
5. **Verify** — one login attempt, with an explicit warning not to retry in a
   loop if it fails (LDAP lockout; the usual cause is username *format*)
6. **Allowlist** — lists every database, filter by name, pick by number. The
   **`dialect` is derived from Superset's own backend field** and the numeric
   `id` is pinned automatically — the two things a newcomer cannot know and
   which otherwise silently break queries
7. **Optional** — install starter monitoring watches (they read Superset
   metadata, so they cost no query budget) and store a Google Chat webhook

Then confirm:

```bash
tathya health                                   # expect auth: ok
tathya sql run "<your db>" "DROP TABLE x"       # expect exit 5, refused
```

<details>
<summary>Configuring by hand instead</summary>

`tathya init` writes a starter `~/.config/tathya/config.toml` to edit. Three
things matter and are easy to get wrong:

- **`base_url` is the origin**, not a page — `https://superset.example.com`, not
  `https://superset.example.com/superset/welcome/`. The CLI appends `/api/v1`.
- **`provider` must be `db` or `ldap`.** Superset's JWT login has no OAuth
  branch, so an SSO-only account cannot obtain a token at all.
- **Set `dialect` per database**, mapped from the `backend` column of
  `tathya db list`: `mysql`→`mysql`, `postgresql`→`postgres`, `mssql`→`tsql`,
  `sqlite`→`sqlite`. Without it, valid dialect-specific SQL (MySQL backtick
  identifiers being the common case) is refused as unparseable.

```toml
[[databases]]
name = "Acme OMS"        # exactly as shown by `tathya db list`
id = 42
dialect = "mysql"
max_rows = 200
```

Prefer an OLAP or read-replica database over a transactional one where both
exist. That single choice does more for safety than any cap in this tool.

</details>

## 4. Reporting to Google Chat (optional)

```bash
tathya notify set-webhook     # paste a space webhook; stored in the Keychain
tathya notify test            # preview only
tathya notify test --confirm  # actually post
```

Anything that posts externally requires `--confirm`; previewing is the default.

## 5. Scheduled monitoring (optional)

```bash
cp launchd/co.tathya.digest.plist ~/Library/LaunchAgents/
# edit the paths inside first
launchctl load ~/Library/LaunchAgents/co.tathya.digest.plist
```

launchd does not read your shell profile, so config and state paths are set in
the plist. These are **LaunchAgents**, not daemons, because reading the
credential needs an unlocked login Keychain.

---

# The safety model

| Control | Behaviour |
|---|---|
| **Database allowlist** | Deny by default. A database Superset exposes is unusable until added to `[[databases]]`. Applies to ad-hoc SQL *and* saved-chart data. |
| **Read-only SQL guard** | Single statement; `SELECT`/`WITH` roots only; no write or DDL node anywhere in the parse tree (including data-modifying CTEs); no `SELECT … INTO`; no `FOR UPDATE`/`FOR SHARE`; no session-stalling functions; no parameter placeholders. Anything unparseable is **refused**, not forwarded. |
| **Row caps** | A `LIMIT` is injected when absent and clamped when too large. `--max-rows` may only tighten it. |
| **Rate limits** | Per-minute token bucket, daily ceiling, and concurrency cap — all enforced **locally, before the request is sent**. Exhaustion is a terminal error, not a wait, so an agent cannot turn a rate limit into a slow flood. |
| **Blackout windows** | Optional clock windows where ad-hoc SQL is refused, e.g. peak hours. |
| **No retry on writes or queries** | Idempotent `GET`s retry with backoff. `POST /sqllab/execute/` never does — a retry runs the query on the database again. |
| **Two-switch write gate** | Writes are a dry run unless `--confirm` **and** `TATHYA_ALLOW_WRITE=1`. Deletes additionally need `allow_delete = true`. |
| **Secret redaction** | Two layers: exact/suffix key-name matching, plus value scanning for DSN passwords and JWTs regardless of key name. |
| **Audit log** | Every call appends one JSON line to `~/.local/state/tathya/audit.jsonl` — actor, guarded SQL, rows, duration, outcome — including refusals. |

Agents should set `TATHYA_ACTOR` so their load is attributable:

```bash
TATHYA_ACTOR=agent:inventory-review tathya sql run ...
```

## Exit codes

Agents branch on these instead of parsing text.

| Code | Meaning | Retry? |
|---|---|---|
| 0 | success | — |
| 2 | usage error | no |
| 3 | config error, e.g. database not allowlisted | no |
| 4 | auth failure | no — re-run `tathya auth login` |
| 5 | **SQL guard refused the statement** | **never** — rewrite the query |
| 6 | **rate limited / blackout** | **never in a loop** — back off |
| 7 | upstream Superset error | maybe, with backoff |
| 8 | write blocked by the gate | no |
| 9 | a watch or digest needs attention (not an error) | — |
| 10 | timeout — the query may still be running | no |

---

# Usage

Output is JSON when stdout is not a terminal and human-readable when it is, so
one command serves both you and an agent. Force with `--json` / `--text`.

## Explore

```bash
tathya db list                       # which databases agents may query
tathya dataset list --search order
tathya dataset get 7                 # columns, types, metrics
tathya dashboard list
tathya dashboard charts my-dashboard
tathya chart data 10                 # run a saved chart's query
```

## Ad-hoc answers

```bash
tathya sql run "Acme OMS" "SELECT status, COUNT(*) FROM orders GROUP BY 1"
tathya sql run "Acme OMS" --file query.sql --max-rows 200
tathya sql run "Acme OMS" "SELECT * FROM orders" --explain-only   # guard decision, no DB contact
tathya sql estimate "Acme OMS" --file query.sql                   # engine cost, where supported
tathya sql run "Acme OMS" "SELECT 1" --format csv
```

`--explain-only` is the cheapest way to check an unfamiliar query: it shows the
guard's rewritten SQL and its warnings without contacting the database.

## Manage dashboards and charts

Dry run unless **both** switches are set.

```bash
tathya chart create --name "Stuck Orders" --dataset-id 7 --viz-type big_number_total
TATHYA_ALLOW_WRITE=1 tathya chart create ... --confirm
TATHYA_ALLOW_WRITE=1 tathya dashboard add-chart 2 11 --confirm
tathya export dashboard 1 --out dashboard.zip
```

## Accountability

```bash
tathya limits                            # remaining query budget, blackout state
tathya audit tail --limit 50
tathya audit tail --outcome guard_rejected
```

---

# Monitoring production without querying it

Superset already records every SQL Lab query it has run and the state of every
configured Alert & Report. Reading *that* gives real visibility for the cost of a
couple of metadata calls, instead of adding load to the databases this tool
exists to protect.

So `ops` and `digest` **consume no query budget** — they cannot put load on a
client database — and they deliberately observe **all** databases, not just
allowlisted ones. The allowlist governs what an agent may *cause*; these only
read the record of what already happened.

```bash
tathya digest                            # exits 9 when something needs attention
tathya digest --since 24h --target gchat --confirm

tathya ops load --since 6h                # volume + avg/p95/max duration per database
tathya ops slow --since 24h --min-seconds 30
tathya ops failures --since 6h
tathya ops queries --database "Acme OMS" --status failed
tathya ops alerts                         # health of Superset's own alerts & reports
```

Digest thresholds live under `[digest]` in the config.

## Alert staleness is judged against each schedule

An active alert that silently stopped evaluating is worse than one that visibly
errors. But staleness cannot be a flat age: a monthly report legitimately sits
idle for weeks and a Friday-only alert for days.

`ops alerts` computes each alert's *previous expected run* from its own crontab,
in its own timezone, and flags it only if it has not evaluated since. On a real
instance this took the "stale" count from **70 false positives to 2 real ones**.
A monitor you learn to ignore is worse than no monitor.

---

# Watches

A watch is a condition evaluated on a schedule that reports on **status
transitions** — so a condition that stays true all day produces one message, not
forty-eight. `renotify_after` opts back into periodic reminders.

Definitions live in `~/.config/tathya/watches/*.yaml`. Examples are in
[`examples/watches/`](examples/watches).

```bash
tathya watch list
tathya watch run --all --due-only        # what the scheduler runs
tathya watch run my-watch --dry-run      # preview the message, send nothing
tathya watch state
```

## Ops-backed watches (preferred)

These monitor production without querying it, so they can poll every 15 minutes.

```yaml
name: slow-queries
description: Queries running longer than 30s anywhere
source:
  type: ops
  signal: slow_queries        # failed_queries | slow_queries | max_query_seconds
  since: 6h                   # query_volume | alert_errors | alert_stale
  threshold_seconds: 30
condition:
  type: threshold
  op: ">"
  value: 5
schedule:
  every: 30m
notify:
  targets: [local]
  renotify_after: 6h
```

## SQL-backed watches

For business conditions only a query can answer. Keep these narrow and
infrequent — they run against a real database.

```yaml
name: stuck-orders
source:
  type: sql              # or: type: chart, chart_id: 10
  database: "Acme OMS"
  sql: |
    SELECT COUNT(*) AS stuck
    FROM orders
    WHERE status = 'APPROVED'
      AND created_at < NOW() - INTERVAL '24 hours'
condition:
  type: threshold        # threshold | rowcount | change | delta_pct
  column: stuck
  op: ">"
  value: 50
schedule:
  every: 30m
notify:
  targets: [local, gchat]
```

## Windowed watches: only what changed since the last run

For "did anything go wrong since we last looked", add `source.window`. Each run
checks the span from where the previous successful run ended up to `now - lag`,
so consecutive runs cover time with no gap and no overlap:

```yaml
name: sync-gaps
source:
  type: sql
  database: "Acme OMS"
  sql_file: sync-gaps.sql      # beside this file; or put the SQL inline with `sql:`
  window:
    lag: 20m                   # leave in-flight work alone
    initial: 30m               # how far the first run looks back
    max: 24h                   # cap on catch-up after a long sleep (reported as truncated)
condition:
  type: rowcount
  op: ">"
  value: 0
notify:
  targets: [local]
  mode: each_alert             # every run that finds rows reports; a clean window stays quiet
  breakdown_column: finding    # summary reads "12 row(s) ...: 9 MISSING, 3 LATE"
  handoff_dir: ~/Tathya/inbox  # each alert also drops one JSON file with its rows here
```

The SQL must use `{{window_start_utc}}` and `{{window_end_utc}}`. They become
`'YYYY-MM-DD HH:MM:SS'` UTC literals; convert them to the database's time zone
in SQL if it stores local time.

- **Window state:** the window advances only after a successful check. A failed
  run is retried next time.
- **Deferred runs:** a run that cannot get a query slot or budget (exit 6) is
  recorded as `deferred`. It sends no notification and leaves the window
  untouched, so the next run covers it.
- **Handoff files:** each one is written beside the folder and renamed in, so a
  folder automation never sees a partial file.

---

# Using this with AI agents

[`skills/tathya-cli/SKILL.md`](skills/tathya-cli/SKILL.md) is a Claude Code skill
that teaches an agent the safe command surface, the exit-code contract, and the
rules that matter — chiefly *never retry on exit 5 or 6*. Install it by linking
into your skills directory:

```bash
ln -s "$PWD/skills/tathya-cli" ~/.claude/skills/tathya-cli
```

---

# Development

```bash
uv sync
uv run pytest -q
```

[`tests/mock_superset.py`](tests/mock_superset.py) is a mock Superset 4.0 API
that **enforces** the contract rather than rubber-stamping it: it requires the
CSRF header and `Referer` on mutating calls, rejects any `sqllab/execute` payload
without a `LIMIT` or containing write keywords, reproduces Superset's
11-character `client_id` limit, and mirrors the real payload shapes (the chart
*detail* endpoint omits `datasource_id`, for instance). Run it standalone to
exercise the CLI without touching a real instance:

```bash
uv run python tests/mock_superset.py 8099
```

**The installed CLI has its own environment.** `uv tool install --editable`
tracks the working tree for *code* but not for dependencies: after `uv add`, the
tool keeps its old environment until reinstalled. A new dependency then works
under `uv run` and fails under `tathya` — silently, if the import sits inside a
`try/except`. Reinstall after changing dependencies:

```bash
uv tool install --editable . --force
```

---

# Known limits

- **`chart data` needs a saved query context.** Superset's
  `/api/v1/chart/{id}/data/` returns `400 "Chart has no query context saved"` for
  charts created by older versions or via the API. Re-saving the chart once in
  the UI populates it; otherwise use `tathya sql run`.
- **Asset exports leak topology, not passwords.** Export YAML contains
  `sqlalchemy_uri` with the password masked by Superset, but username, host,
  port, and database name are in the clear. Review before committing.
- **`dashboard add-chart` appends a full-width row.** Finer layout needs
  `--position-json-file` or the Superset UI.
- **Cost estimation is engine-dependent.** `sql estimate` reports unsupported
  rather than guessing where the engine cannot cost a query.
- **`/api/v1/me/` is absent on some deployments.** Identity falls back to the JWT
  subject claim; health does not depend on that endpoint.
- **macOS only**, because secret storage uses the macOS Keychain.

# License

[Apache License 2.0](LICENSE) — matching Apache Superset itself.
