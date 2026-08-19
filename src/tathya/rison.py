"""Minimal Rison encoder.

Superset's list endpoints take their query in the ``q`` parameter encoded as
Rison (via the ``prison`` library server-side), not JSON - ``q={"page":0}`` is
rejected. Only the subset needed for filters, column selection, ordering, and
paging is implemented.

Encoding rules used here:
  object -> ``(k:v,k2:v2)``      array -> ``!(a,b,c)``
  true/false/null -> ``!t``/``!f``/``!n``
  strings -> bare when unambiguous, else ``'quoted'`` with ``!`` and ``'`` escaped
"""

from __future__ import annotations

import re
from typing import Any

#: Conservative bare-identifier test. Anything else gets quoted, which is always
#: safe - including strings that would otherwise be read back as numbers.
_BARE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(text: str) -> str:
    escaped = text.replace("!", "!!").replace("'", "!'")
    return f"'{escaped}'"


def dumps(value: Any) -> str:
    if value is None:
        return "!n"
    if value is True:
        return "!t"
    if value is False:
        return "!f"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return value if _BARE_RE.match(value) else _quote(value)
    if isinstance(value, dict):
        inner = ",".join(f"{dumps(k)}:{dumps(v)}" for k, v in value.items())
        return f"({inner})"
    if isinstance(value, (list, tuple)):
        return "!(" + ",".join(dumps(item) for item in value) + ")"
    raise TypeError(f"Cannot rison-encode {type(value).__name__}")


def query(
    *,
    columns: list[str] | None = None,
    filters: list[dict] | None = None,
    order_column: str | None = None,
    order_direction: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> str:
    """Build a Superset ``q`` value, omitting unset parts."""
    payload: dict[str, Any] = {}
    if columns:
        payload["columns"] = columns
    if filters:
        payload["filters"] = filters
    if order_column:
        payload["order_column"] = order_column
    if order_direction:
        payload["order_direction"] = order_direction
    if page is not None:
        payload["page"] = page
    if page_size is not None:
        payload["page_size"] = page_size
    return dumps(payload)


def contains_filter(column: str, value: str) -> dict:
    return {"col": column, "opr": "ct", "value": value}


def equals_filter(column: str, value: Any) -> dict:
    return {"col": column, "opr": "eq", "value": value}
