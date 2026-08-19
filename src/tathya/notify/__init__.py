"""Reporting channels.

A watch that fires produces one *event* dict; each channel renders it. Channels
never raise on delivery failure alone - a failed notification must not mask the
alert itself, so failures are returned in the result and surfaced by the caller.
"""

from __future__ import annotations

from typing import Any

from tathya.config import Config
from tathya.errors import ConfigError
from tathya.notify import gchat, local

CHANNELS = ("local", "gchat")


def deliver(
    event: dict,
    targets: tuple[str, ...] | list[str],
    config: Config,
    *,
    dry_run: bool = False,
) -> list[dict]:
    results: list[dict] = []
    for target in targets:
        if target == "local":
            results.append(local.send(event, dry_run=dry_run))
        elif target == "gchat":
            results.append(gchat.send(event, config, dry_run=dry_run))
        else:
            raise ConfigError(
                f"Unknown notify target {target!r}.", known=list(CHANNELS)
            )
    return results


def deliver_text(
    text: str,
    targets: tuple[str, ...] | list[str],
    config: Config,
    *,
    dry_run: bool = False,
    title: str = "Tathya digest",
    thread_key: str = "",
) -> list[dict]:
    """Send one pre-rendered message to each channel."""
    results: list[dict] = []
    for target in targets:
        if target == "local":
            results.append(local.send_text(text, dry_run=dry_run, title=title))
        elif target == "gchat":
            results.append(gchat.send_text(text, config, dry_run=dry_run, thread_key=thread_key))
        else:
            raise ConfigError(f"Unknown notify target {target!r}.", known=list(CHANNELS))
    return results
