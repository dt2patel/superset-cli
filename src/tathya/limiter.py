"""Local rate limiting, concurrency capping, and blackout windows.

Enforcement happens **on this machine, before the request is sent**. That is
deliberate: a server-side limit still requires Tathya to accept and begin the
query, and the failure mode we are defending against is an agent retry loop
issuing the same expensive query dozens of times against a live client OMS
read-replica. Locally-enforced caps make that physically impossible.

Exhaustion is reported as a terminal error, not a sleep-and-continue, so an
agent cannot convert a rate limit into a slow flood.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from tathya.config import SafetyConfig
from tathya.errors import LimitError

GLOBAL_SCOPE = "global"

#: Slots this process currently holds. Record locks are per-process, so the
#: kernel cannot tell us about our own contention.
_HELD_SLOTS: set[int] = set()

#: One descriptor per concurrency file, kept open for the process lifetime.
#: Closing any descriptor to a file releases every record lock the process holds
#: on that file, so descriptors must not be opened and closed per acquisition.
_CONCURRENCY_FDS: dict[str, int] = {}


def _concurrency_fd(path: Path) -> int:
    key = str(path)
    fd = _CONCURRENCY_FDS.get(key)
    if fd is None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        _CONCURRENCY_FDS[key] = fd
    return fd

_DAY_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_WINDOW_RE = re.compile(
    r"^\s*(?P<days>[A-Za-z,\-\*]+)\s+(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})\s*$"
)


def _parse_days(token: str) -> set[int]:
    token = token.strip().lower()
    if token in {"*", "daily", "all"}:
        return set(range(7))
    days: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if "-" in part:
            start_name, _, end_name = part.partition("-")
            start, end = _DAY_NAMES.get(start_name[:3]), _DAY_NAMES.get(end_name[:3])
            if start is None or end is None:
                raise LimitError(f"Unparseable blackout day range {part!r}")
            index = start
            while True:
                days.add(index)
                if index == end:
                    break
                index = (index + 1) % 7
        else:
            value = _DAY_NAMES.get(part[:3])
            if value is None:
                raise LimitError(f"Unparseable blackout day {part!r}")
            days.add(value)
    return days


def _minutes(value: str) -> int:
    hours, _, mins = value.partition(":")
    return int(hours) * 60 + int(mins)


def in_blackout(windows: tuple[str, ...], now: datetime | None = None) -> str | None:
    """Return the first matching window spec, or None. Local time."""
    if not windows:
        return None
    moment = now or datetime.now()
    current = moment.hour * 60 + moment.minute
    for spec in windows:
        match = _WINDOW_RE.match(spec)
        if not match:
            raise LimitError(
                f"Unparseable blackout window {spec!r}; expected e.g. 'Mon-Fri 09:00-11:00'"
            )
        if moment.weekday() not in _parse_days(match.group("days")):
            continue
        start, end = _minutes(match.group("start")), _minutes(match.group("end"))
        inside = start <= current < end if start <= end else (current >= start or current < end)
        if inside:
            return spec
    return None


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Limiter:
    def __init__(self, safety: SafetyConfig, state_dir: Path) -> None:
        self.safety = safety
        self.state_path = state_dir / "limits.json"
        self.concurrency_path = state_dir / "concurrency.lock"

    # ------------------------------------------------------------------ state
    def _read(self, fd: int) -> dict:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 1 << 20).decode() or "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _write(self, fd: int, state: dict) -> None:
        payload = json.dumps(state).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, payload)

    def _refill(self, bucket: dict, now: float) -> dict:
        capacity = float(self.safety.max_queries_per_minute)
        rate = capacity / 60.0
        tokens = float(bucket.get("tokens", capacity))
        updated = float(bucket.get("updated", now))
        tokens = min(capacity, tokens + max(0.0, now - updated) * rate)
        return {"tokens": tokens, "updated": now}

    # ----------------------------------------------------------------- checks
    def check_blackout(self, now: datetime | None = None) -> None:
        window = in_blackout(self.safety.blackout_windows, now)
        if window:
            raise LimitError(
                f"Ad-hoc queries are blocked during the blackout window {window!r}.",
                window=window,
                hint="Wait for the window to close, or use an existing chart instead of ad-hoc SQL.",
            )

    def _consume(self, scope: str) -> None:
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        with _locked(self.state_path) as fd:
            state = self._read(fd)
            buckets = state.setdefault("buckets", {})
            daily = state.setdefault("daily", {})
            if daily.get("date") != today:
                daily.clear()
                daily["date"] = today
                daily["counts"] = {}
            counts = daily.setdefault("counts", {})

            # Daily ceiling first - it is the coarsest and most protective.
            used_today = int(counts.get(GLOBAL_SCOPE, 0))
            if used_today >= self.safety.max_queries_per_day:
                raise LimitError(
                    f"Daily query cap reached ({used_today}/{self.safety.max_queries_per_day}).",
                    scope=GLOBAL_SCOPE,
                    resets="local midnight",
                )

            refilled: dict[str, dict] = {}
            for key in (GLOBAL_SCOPE, scope):
                bucket = self._refill(buckets.get(key, {}), now)
                if bucket["tokens"] < 1.0:
                    deficit = 1.0 - bucket["tokens"]
                    retry_after = deficit / (self.safety.max_queries_per_minute / 60.0)
                    raise LimitError(
                        f"Rate limit reached for {key!r} "
                        f"({self.safety.max_queries_per_minute}/min).",
                        scope=key,
                        retry_after_seconds=round(retry_after, 1),
                        hint="Do not retry in a loop; this cap protects a production replica.",
                    )
                refilled[key] = bucket

            for key, bucket in refilled.items():
                bucket["tokens"] -= 1.0
                buckets[key] = bucket
                counts[key] = int(counts.get(key, 0)) + 1
            self._write(fd, state)

    @contextlib.contextmanager
    def _slot(self, scope: str) -> Iterator[None]:
        """Hold one of ``max_concurrent_queries`` slots.

        Two POSIX record-lock hazards are handled explicitly:

        * Record locks are owned by the *process*, so a second ``lockf`` on the
          same byte from the same process succeeds. The watch runner evaluates
          several watches in one process, so in-process slots are tracked in
          ``_HELD_SLOTS`` as well.
        * Closing *any* descriptor for a file drops every lock the process holds
          on it. One descriptor per path is therefore opened once and kept for
          the process lifetime instead of per acquisition.
        """
        slots = max(1, self.safety.max_concurrent_queries)
        fd = _concurrency_fd(self.concurrency_path)
        held: int | None = None
        try:
            for index in range(slots):
                if index in _HELD_SLOTS:
                    continue  # already ours; re-locking it would falsely succeed
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, index)
                except OSError:
                    continue
                held = index
                _HELD_SLOTS.add(index)
                break
            if held is None:
                raise LimitError(
                    f"All {slots} concurrent query slot(s) are in use.",
                    scope=scope,
                    hint="Another query is already running; wait for it rather than retrying.",
                )
            yield
        finally:
            if held is not None:
                _HELD_SLOTS.discard(held)
                with contextlib.suppress(OSError):
                    fcntl.lockf(fd, fcntl.LOCK_UN, 1, held)

    @contextlib.contextmanager
    def acquire(self, scope: str, *, check_blackout: bool = True) -> Iterator[None]:
        """Full gate for one outbound query: blackout, daily, rate, concurrency."""
        if check_blackout:
            self.check_blackout()
        self._consume(scope)
        with self._slot(scope):
            yield

    def status(self) -> dict:
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        with _locked(self.state_path) as fd:
            state = self._read(fd)
        buckets = state.get("buckets", {})
        daily = state.get("daily", {})
        counts = daily.get("counts", {}) if daily.get("date") == today else {}
        return {
            "limits": {
                "max_queries_per_minute": self.safety.max_queries_per_minute,
                "max_concurrent_queries": self.safety.max_concurrent_queries,
                "max_queries_per_day": self.safety.max_queries_per_day,
            },
            "tokens_available": {
                key: round(self._refill(bucket, now)["tokens"], 2)
                for key, bucket in buckets.items()
            },
            "used_today": counts,
            "blackout_windows": list(self.safety.blackout_windows),
            "in_blackout": in_blackout(self.safety.blackout_windows),
        }
