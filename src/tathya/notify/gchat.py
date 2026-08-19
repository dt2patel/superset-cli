"""Google Chat webhook delivery.

A Chat webhook URL embeds ``key`` and ``token`` query parameters and is
therefore a credential in its own right: anyone holding it can post into the
space. It lives in the Keychain, never in the config file or a watch YAML, and
is redacted out of every log line.
"""

from __future__ import annotations

import httpx

from tathya.config import Config
from tathya.errors import ConfigError
from tathya.keychain import GCHAT_WEBHOOK_ACCOUNT, get_secret

STATUS_ICON = {
    "alert": "\U0001F534",      # red circle
    "error": "⚠️",    # warning
    "ok": "\U0001F7E2",         # green circle
    "recovered": "\U0001F7E2",
}


def webhook_url() -> str:
    url = get_secret(GCHAT_WEBHOOK_ACCOUNT)
    if not url:
        raise ConfigError(
            "No Google Chat webhook is stored.",
            fix="Run `tathya notify set-webhook` and paste the space's webhook URL at the prompt.",
        )
    return url


def format_text(event: dict) -> str:
    """Google Chat markup: *bold*, _italic_, `code`, <url|label>."""
    status = str(event.get("status", "")).lower()
    icon = STATUS_ICON.get(status, "")
    name = event.get("watch", "watch")
    lines = [f"{icon} *{status.upper()}* — {name}"]

    if event.get("description"):
        lines.append(f"_{event['description']}_")

    facts = []
    if event.get("database"):
        facts.append(f"`{event['database']}`")
    if event.get("summary"):
        facts.append(event["summary"])
    if facts:
        lines.append(" · ".join(facts))

    previous = event.get("previous_status")
    if previous and previous != status:
        lines.append(f"changed from *{str(previous).upper()}*")

    if event.get("rows") is not None:
        lines.append(f"rows returned: {event['rows']}")
    if event.get("link"):
        lines.append(f"<{event['link']}|open in Tathya>")
    if event.get("actor"):
        lines.append(f"_reported by tathya-cli ({event['actor']})_")
    return "\n".join(lines)


def send(event: dict, config: Config, *, dry_run: bool = False) -> dict:
    text = format_text(event)
    thread_key = config.reporting.gchat_thread_key or event.get("watch")
    if dry_run:
        return {
            "target": "gchat",
            "delivered": False,
            "dry_run": True,
            "space_label": config.reporting.gchat_space_label,
            "thread_key": thread_key,
            "preview": text,
        }

    url = webhook_url()
    params = {}
    if thread_key:
        # Keep one thread per watch so repeat alerts stay together in the space.
        params["threadKey"] = thread_key
        params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    try:
        response = httpx.post(url, params=params, json={"text": text}, timeout=20.0)
    except httpx.HTTPError as exc:
        # A delivery failure must not mask the alert it was carrying.
        return {"target": "gchat", "delivered": False, "error": str(exc)}
    if response.status_code >= 400:
        return {
            "target": "gchat",
            "delivered": False,
            "error": f"HTTP {response.status_code}",
            "body": response.text[:300],
        }
    return {
        "target": "gchat",
        "delivered": True,
        "space_label": config.reporting.gchat_space_label,
        "thread_key": thread_key,
    }


def send_text(text: str, config: Config, *, dry_run: bool = False, thread_key: str = "") -> dict:
    """Post a pre-rendered message (used by the digest)."""
    if dry_run:
        return {"target": "gchat", "delivered": False, "dry_run": True,
                "space_label": config.reporting.gchat_space_label, "preview": text}
    url = webhook_url()
    params = {}
    key = thread_key or config.reporting.gchat_thread_key
    if key:
        params["threadKey"] = key
        params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    try:
        response = httpx.post(url, params=params, json={"text": text}, timeout=20.0)
    except httpx.HTTPError as exc:
        return {"target": "gchat", "delivered": False, "error": str(exc)}
    if response.status_code >= 400:
        return {"target": "gchat", "delivered": False,
                "error": f"HTTP {response.status_code}", "body": response.text[:300]}
    return {"target": "gchat", "delivered": True,
            "space_label": config.reporting.gchat_space_label}
