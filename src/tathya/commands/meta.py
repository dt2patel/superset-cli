"""Setup, auth, health, limits, audit, and notification-channel commands."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from tathya import config as config_mod
from tathya.context import Context
from tathya.errors import EXIT_AUTH, EXIT_OK, ConfigError, UsageError
from tathya.keychain import (
    GCHAT_WEBHOOK_ACCOUNT,
    account_for_password,
    delete_secret,
    get_secret,
    set_secret_interactive,
)
from tathya.notify import deliver
from tathya.output import render_table


def _require_tty(what: str) -> None:
    if not sys.stdin.isatty():
        raise UsageError(
            f"{what} needs an interactive terminal so the secret can be typed directly.",
            note="Run this yourself in a terminal; it is deliberately not scriptable.",
        )


def cmd_init(ctx: Context) -> int:
    path = config_mod.config_path()
    if path.exists() and not ctx.args.force:
        raise UsageError(
            f"Config already exists at {path}; pass --force to overwrite it.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_mod.DEFAULT_CONFIG_TOML)
    path.chmod(0o600)
    watches = config_mod.watches_dir()
    watches.mkdir(parents=True, exist_ok=True)
    def human(data, line):
        line(f"Config written to {data['config']}")
        line("")
        line("Next:")
        for step in data["next_steps"]:
            line(f"  - {step}")

    return ctx.emitter.ok(
        "init",
        {
            "config": str(path),
            "watches_dir": str(watches),
            "next_steps": [
                "Run `tathya setup` to fill this in interactively (recommended).",
                f"Or edit {path} by hand: instance.base_url, instance.username, "
                "and a [[databases]] entry per database agents may query.",
            ],
        },
        human=human,
    )


def cmd_auth_login(ctx: Context) -> int:
    username = ctx.config.instance.username
    account = account_for_password(username)
    if ctx.args.reuse_stored and get_secret(account):
        stored = True
    else:
        _require_tty("Storing the service-account password")
        set_secret_interactive(
            account, f"Superset password for {username!r} at {ctx.config.instance.base_url}"
        )
        stored = True
    session = ctx.client.auth.login()
    return ctx.emitter.ok(
        "auth.login",
        {
            "base_url": ctx.config.instance.base_url,
            "username": username,
            "password_stored_in_keychain": stored,
            "access_token_acquired": bool(session.access_token),
            "refresh_token_acquired": bool(session.refresh_token),
        },
    )


def cmd_auth_status(ctx: Context) -> int:
    from tathya.rison import query as rison_query

    status = ctx.client.auth.status()
    if ctx.args.probe:
        # Probe a listing endpoint, not /api/v1/me/ - that route is absent on
        # some deployments (Tathya returns 401 for it regardless of the token),
        # which would make a perfectly working session look broken.
        try:
            payload = ctx.client.get(
                "/api/v1/dashboard/", params={"q": rison_query(page=0, page_size=1)}
            )
            status["probe"] = {
                "ok": True,
                "endpoint": "/api/v1/dashboard/",
                "visible_dashboards": payload.get("count"),
            }
        except Exception as exc:  # surfaced, not fatal - status should still print
            status["probe"] = {"ok": False, "error": str(getattr(exc, "message", exc))}
    return ctx.emitter.ok("auth.status", status)


def cmd_auth_logout(ctx: Context) -> int:
    cleared = ctx.client.auth.store.clear()
    removed = False
    if ctx.args.forget_password:
        removed = delete_secret(account_for_password(ctx.config.instance.username))
    return ctx.emitter.ok(
        "auth.logout",
        {"session_cleared": cleared, "keychain_password_removed": removed},
    )


def cmd_health(ctx: Context) -> int:
    from tathya.rison import query as rison_query

    report: dict = {
        "base_url": ctx.config.instance.base_url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        raw = ctx.client.get("/health", expect_json=False)
        report["health_endpoint"] = raw.decode(errors="replace").strip()[:40]
    except Exception as exc:
        report["health_endpoint"] = f"unavailable: {getattr(exc, 'message', exc)}"

    # Identity comes from the JWT subject, because /api/v1/me/ is not available
    # on every deployment. Try the endpoint, but never fail health over it.
    auth_status = ctx.client.auth.status()
    identity: dict = {
        "config_username": auth_status.get("username"),
        "superset_user_id": auth_status.get("superset_user_id"),
        "source": "jwt_claim",
    }
    try:
        result = (ctx.client.get("/api/v1/me/") or {}).get("result") or {}
        if result:
            identity.update(
                {key: result.get(key) for key in ("username", "first_name", "last_name", "email")}
            )
            identity["source"] = "/api/v1/me/"
    except Exception as exc:
        identity["me_endpoint"] = f"unavailable ({getattr(exc, 'message', exc)})"
    report["identity"] = identity

    counts: dict[str, object] = {}
    count_errors: set[str] = set()
    for label, path in (
        ("dashboards", "/api/v1/dashboard/"),
        ("charts", "/api/v1/chart/"),
        ("datasets", "/api/v1/dataset/"),
        ("databases", "/api/v1/database/"),
    ):
        try:
            payload = ctx.client.get(path, params={"q": rison_query(page=0, page_size=1)})
            counts[label] = payload.get("count")
        except Exception as exc:
            # Collapse identical failures - four copies of one auth error is noise.
            counts[label] = "unavailable"
            count_errors.add(str(getattr(exc, "message", exc)))
    report["visible_counts"] = counts
    if count_errors:
        report["count_errors"] = sorted(count_errors)
    # Authentication is proven by a successful authenticated call, not by /me/.
    report["authenticated"] = any(isinstance(v, int) for v in counts.values())
    report["allowlisted_databases"] = [rule.name for rule in ctx.config.databases]
    report["writes_enabled"] = __import__("os").environ.get("TATHYA_ALLOW_WRITE") == "1"

    def human(data, line):
        who = data.get("identity") or {}
        line(f"instance   : {data['base_url']}")
        line(f"health     : {data.get('health_endpoint')}")
        line(f"auth       : {'ok' if data.get('authenticated') else 'FAILED'}")
        line(f"user       : {who.get('username') or who.get('config_username')} "
             f"(superset id {who.get('superset_user_id')}, via {who.get('source')})")
        line("counts     : " + ", ".join(f"{k}={v}" for k, v in data["visible_counts"].items()))
        line(f"allowlisted: {', '.join(data['allowlisted_databases']) or '(none - sql run will refuse)'}")
        line(f"writes     : {'enabled' if data['writes_enabled'] else 'disabled'}")
        for problem in data.get("count_errors") or []:
            line(f"problem    : {problem}")

    # A health check that cannot authenticate must not report success: an agent or
    # a monitoring script branches on this exit code.
    exit_code = EXIT_OK if report["authenticated"] else EXIT_AUTH
    return ctx.emitter.ok("health", report, human=human, exit_code=exit_code)


def cmd_limits(ctx: Context) -> int:
    return ctx.emitter.ok("limits", ctx.limiter.status())


def cmd_audit_tail(ctx: Context) -> int:
    rows = ctx.audit.tail(
        ctx.args.limit,
        actor_filter=ctx.args.actor_filter,
        command_filter=ctx.args.command,
        outcome_filter=ctx.args.outcome,
    )

    def human(data, line):
        render_table(
            data,
            ["ts", "actor", "command", "database", "rows", "duration_ms", "outcome"],
            line,
        )

    return ctx.emitter.ok("audit.tail", rows, human=human)


def cmd_notify_set_webhook(ctx: Context) -> int:
    _require_tty("Storing the Google Chat webhook")
    print(
        "Paste the Google Chat space webhook URL (Space > Apps & integrations > Webhooks).\n"
        "It is a credential - anyone with it can post to that space - so it goes to the Keychain."
    )
    set_secret_interactive(GCHAT_WEBHOOK_ACCOUNT, "Google Chat webhook URL")
    stored = get_secret(GCHAT_WEBHOOK_ACCOUNT) or ""
    if stored and "chat.googleapis.com" not in stored:
        return ctx.emitter.ok(
            "notify.set-webhook",
            {"stored": True, "looks_like_google_chat": False},
            warnings=[
                "The stored value does not contain chat.googleapis.com; double-check it is the right URL."
            ],
        )
    return ctx.emitter.ok(
        "notify.set-webhook", {"stored": bool(stored), "looks_like_google_chat": True}
    )


def cmd_notify_test(ctx: Context) -> int:
    """Send (or preview) a test message.

    Defaults to a preview. Actually posting into a shared space is an outward
    facing action, so it needs --confirm.
    """
    event = {
        "watch": "notify-test",
        "description": "Test message from tathya-cli",
        "status": "ok",
        "summary": "If you can read this, the channel works.",
        "rows": 0,
        "link": ctx.config.instance.base_url,
        "actor": __import__("tathya.audit", fromlist=["actor"]).actor(),
    }
    targets = tuple(ctx.args.target or ctx.config.reporting.default_targets)
    dry_run = not ctx.confirmed
    results = deliver(event, targets, ctx.config, dry_run=dry_run)
    return ctx.emitter.ok(
        "notify.test",
        {"targets": list(targets), "dry_run": dry_run, "deliveries": results},
        warnings=(
            ["Preview only - nothing was sent. Re-run with --confirm to actually post."]
            if dry_run
            else []
        ),
    )
