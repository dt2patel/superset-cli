"""Local reporting: an append-only event log plus a desktop notification.

Nothing leaves the machine. This is the default channel and stays enabled even
when a remote channel is configured, so there is always a local record of what
was reported.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

from tathya.config import state_dir
from tathya.redact import redact

EVENTS_FILENAME = "events.jsonl"


def _append(event: dict) -> str:
    path = state_dir() / EVENTS_FILENAME
    line = json.dumps(redact(event), default=str, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode())
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return str(path)


def _desktop_notification(title: str, message: str) -> bool:
    """Best-effort macOS banner. Text is passed on stdin, never interpolated
    into a shell command, so quotes in a value cannot break out."""
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    script = (
        "on run argv\n"
        '  display notification (item 1 of argv) with title (item 2 of argv)\n'
        "end run"
    )
    try:
        proc = subprocess.run(
            [osascript, "-", message, title],
            input=script,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def format_line(event: dict) -> str:
    status = str(event.get("status", "?")).upper()
    name = event.get("watch", "watch")
    summary = event.get("summary") or ""
    return f"[{status}] {name}: {summary}".strip()


def send(event: dict, *, dry_run: bool = False) -> dict:
    payload = dict(event)
    payload.setdefault("delivered_at", datetime.now(timezone.utc).isoformat())
    if dry_run:
        return {
            "target": "local",
            "delivered": False,
            "dry_run": True,
            "preview": format_line(payload),
        }
    path = _append(payload)
    notified = _desktop_notification(
        f"Tathya: {payload.get('status', '').upper()} {payload.get('watch', '')}".strip(),
        payload.get("summary") or format_line(payload),
    )
    return {
        "target": "local",
        "delivered": True,
        "event_log": path,
        "desktop_notification": notified,
    }


def send_text(text: str, *, dry_run: bool = False, title: str = "Tathya digest") -> dict:
    """Append a pre-rendered message to the event log (used by the digest)."""
    if dry_run:
        return {"target": "local", "delivered": False, "dry_run": True, "preview": text}
    path = _append({"kind": "digest", "text": text,
                    "delivered_at": datetime.now(timezone.utc).isoformat()})
    first = next((line for line in text.splitlines() if line.strip()), title)
    return {"target": "local", "delivered": True, "event_log": path,
            "desktop_notification": _desktop_notification(title, first[:180])}
