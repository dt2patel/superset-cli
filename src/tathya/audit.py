"""Append-only audit log.

Every command an agent runs lands here as one JSON line: who claimed to run it
(``TATHYA_ACTOR``), what SQL was submitted after guarding, how many rows came
back, and how long it took. This is the record you read after the fact to answer
"what did the agents actually do to the replica", so it is written even when the
command fails, and always passes through redaction first.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tathya.redact import redact

MAX_SQL_CHARS = 4000


def actor() -> str:
    """Who is running this. Agents should set TATHYA_ACTOR to identify themselves."""
    return os.environ.get("TATHYA_ACTOR") or os.environ.get("USER") or "unknown"


def sql_fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.strip().encode()).hexdigest()[:16]


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, **fields: Any) -> dict:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor(),
            "pid": os.getpid(),
        }
        entry.update(fields)
        if isinstance(entry.get("sql"), str) and len(entry["sql"]) > MAX_SQL_CHARS:
            entry["sql"] = entry["sql"][:MAX_SQL_CHARS] + "...[truncated]"
        entry = redact(entry)
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode())
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return entry

    def tail(
        self,
        limit: int = 20,
        *,
        actor_filter: str | None = None,
        command_filter: str | None = None,
        outcome_filter: str | None = None,
    ) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        with self.path.open() as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if actor_filter and row.get("actor") != actor_filter:
                    continue
                if command_filter and not str(row.get("command", "")).startswith(command_filter):
                    continue
                if outcome_filter and row.get("outcome") != outcome_filter:
                    continue
                rows.append(row)
        return rows[-limit:]
