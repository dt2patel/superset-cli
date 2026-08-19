"""Output shaping: machine envelope for agents, terse text for humans.

Default is chosen by whether stdout is a tty, so the same command is readable
when you run it and parseable when an agent runs it.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, Sequence

from tathya.errors import EXIT_OK, TathyaError
from tathya.redact import redact

HumanRenderer = Callable[[Any, Callable[[str], None]], None]


class Emitter:
    def __init__(self, mode: str = "auto", stream: Any = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if mode == "json":
            self.as_json = True
        elif mode == "text":
            self.as_json = False
        else:
            self.as_json = not getattr(self.stream, "isatty", lambda: False)()

    def line(self, text: str = "") -> None:
        print(text, file=self.stream)

    def ok(
        self,
        command: str,
        data: Any = None,
        warnings: Sequence[str] = (),
        human: HumanRenderer | None = None,
        exit_code: int = EXIT_OK,
        **extra: Any,
    ) -> int:
        payload = redact(data)
        if self.as_json:
            envelope = {"ok": True, "command": command, "data": payload}
            if warnings:
                envelope["warnings"] = list(warnings)
            envelope.update({k: redact(v) for k, v in extra.items()})
            self.line(json.dumps(envelope, indent=2, default=str))
        else:
            if human is not None:
                human(payload, self.line)
            elif payload is not None:
                self.line(json.dumps(payload, indent=2, default=str))
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
        return exit_code

    def error(self, command: str, err: TathyaError) -> int:
        if self.as_json:
            self.line(
                json.dumps(
                    {
                        "ok": False,
                        "command": command,
                        "error": redact(err.to_dict()),
                        "exit_code": err.exit_code,
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(f"error [{err.kind}]: {err.message}", file=sys.stderr)
            for key, value in (err.details or {}).items():
                print(f"  {key}: {json.dumps(redact(value), default=str)}", file=sys.stderr)
            if err.exit_code in {5, 6}:
                print("  (terminal - do not retry; change the query or wait)", file=sys.stderr)
        return err.exit_code


def render_table(rows: Iterable[dict], columns: Sequence[str], line: Callable[[str], None]) -> None:
    """Minimal fixed-width table for tty output."""
    rows = list(rows)
    if not rows:
        line("(no rows)")
        return
    widths = {col: len(col) for col in columns}
    cells: list[dict[str, str]] = []
    for row in rows:
        rendered = {}
        for col in columns:
            text = "" if row.get(col) is None else str(row.get(col))
            text = text.replace("\n", " ")
            if len(text) > 60:
                text = text[:57] + "..."
            rendered[col] = text
            widths[col] = max(widths[col], len(text))
        cells.append(rendered)
    line("  ".join(col.ljust(widths[col]) for col in columns))
    line("  ".join("-" * widths[col] for col in columns))
    for rendered in cells:
        line("  ".join(rendered[col].ljust(widths[col]) for col in columns))
