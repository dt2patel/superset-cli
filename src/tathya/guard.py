"""The SQL guard: decide whether a statement may reach a client database.

Design stance is **fail closed**. Anything the parser cannot fully understand is
rejected rather than forwarded, because Tathya's connections terminate on live
per-client production OMS read-replicas. A false rejection costs an agent one
retry with better SQL; a false acceptance can cost a client a replica.

Enforced, in order:
  1. exactly one statement (defeats ``SELECT 1; DROP TABLE ...``)
  2. the root must be a read statement
  3. no write/DDL node anywhere in the tree - including inside a CTE, which is
     how Postgres smuggles ``WITH d AS (DELETE ... RETURNING *) SELECT * FROM d``
  4. no ``SELECT ... INTO`` (parses as a Select, so type-checking alone misses it)
  5. no ``FOR UPDATE`` / ``FOR SHARE`` - those take locks on the source database
  6. no denied functions (``pg_sleep``, ``benchmark``, ...)
  7. a row LIMIT is present, injected if absent and clamped if too large
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

#: Nodes that write, change structure, or change session state. Checked across
#: the whole tree, not just the root.
FORBIDDEN_NODES: dict[type, str] = {
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
    exp.Drop: "DROP",
    exp.Create: "CREATE",
    exp.Alter: "ALTER",
    exp.TruncateTable: "TRUNCATE",
    exp.Merge: "MERGE",
    exp.Grant: "GRANT",
    exp.Set: "SET",
    exp.Use: "USE",
    exp.Copy: "COPY",
    # sqlglot represents anything it cannot parse as a Command. Rejecting it is
    # what makes this guard fail closed instead of forwarding unknown syntax.
    exp.Command: "unsupported or unparsed statement",
}

READ_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)

def _selects_star(select: exp.Select) -> bool:
    """True for ``SELECT *`` / ``SELECT t.*`` but not ``SELECT COUNT(*)``."""
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            return True
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return True
    return False


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool
    original_sql: str
    sql: str = ""
    applied_limit: int | None = None
    limit_source: str = ""  # existing | injected | clamped
    reason: str | None = None
    rule: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "sql": self.sql,
            "applied_limit": self.applied_limit,
            "limit_source": self.limit_source,
            "reason": self.reason,
            "rule": self.rule,
            "warnings": list(self.warnings),
        }


def _reject(sql: str, reason: str, rule: str) -> GuardVerdict:
    return GuardVerdict(ok=False, original_sql=sql, reason=reason, rule=rule)


def _literal_limit(root: exp.Expression) -> int | None:
    """Return the root LIMIT when it is a plain integer literal, else None."""
    node = root.args.get("limit")
    if node is None:
        return None
    expression = getattr(node, "expression", None)
    if expression is None:
        return None
    value = getattr(expression, "this", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _apply_limit(root: exp.Expression, limit: int, dialect: str | None) -> exp.Expression:
    limiter = getattr(root, "limit", None)
    if callable(limiter):
        try:
            return limiter(limit)
        except Exception:  # pragma: no cover - dialect-specific builder gaps
            pass
    wrapped = f"SELECT * FROM ({root.sql(dialect=dialect)}) AS _tathya_guard LIMIT {limit}"
    return sqlglot.parse_one(wrapped, read=dialect)


def _collect_function_names(root: exp.Expression) -> set[str]:
    names: set[str] = set()
    for node in root.find_all(exp.Anonymous):
        this = node.args.get("this")
        if isinstance(this, str):
            names.add(this.lower())
    for node in root.find_all(exp.Func):
        for name in type(node).sql_names():
            names.add(name.lower())
    return names


def analyze(
    sql: str,
    *,
    max_rows: int,
    dialect: str | None = None,
    denied_functions: tuple[str, ...] = (),
) -> GuardVerdict:
    original = sql
    text = (sql or "").strip()
    if not text:
        return _reject(original, "Empty statement.", "empty")

    try:
        parsed = sqlglot.parse(text, read=dialect)
    except sqlglot.errors.ParseError as exc:
        hint = (
            " If this is valid dialect-specific SQL (MySQL backtick identifiers are the"
            f" usual case), set `dialect` for this database in config; parsed as"
            f" {dialect or 'generic SQL'}."
        )
        return _reject(
            original,
            f"Statement could not be parsed, so it cannot be verified as read-only: {exc}.{hint}",
            "unparseable",
        )

    statements = [
        node
        for node in parsed
        if node is not None and not isinstance(node, getattr(exp, "Semicolon", ()))
    ]
    if not statements:
        return _reject(original, "No statement found.", "empty")
    if len(statements) > 1:
        return _reject(
            original,
            f"Only one statement may be submitted; found {len(statements)}. "
            "Stacked statements are refused outright.",
            "multiple_statements",
        )

    root = statements[0]
    if not isinstance(root, READ_ROOTS):
        return _reject(
            original,
            f"Only read statements are allowed; this is {type(root).__name__.upper()}.",
            "not_a_read",
        )

    for node_type, label in FORBIDDEN_NODES.items():
        found = next(root.find_all(node_type), None)
        if found is not None or isinstance(root, node_type):
            return _reject(
                original,
                f"Statement contains {label}, which can modify the source database.",
                "forbidden_node",
            )

    # Parameter placeholders (?, :name, %s, @var) mean the statement is not
    # complete literal SQL. There is no binding step on this path, so it would
    # fail opaquely at the database - and a caller holding placeholders is one
    # step away from interpolating values into SQL by hand.
    for node_type in (exp.Placeholder, exp.Parameter, getattr(exp, "SessionParameter", ())):
        if node_type and next(root.find_all(node_type), None) is not None:
            return _reject(
                original,
                "Statement contains a parameter placeholder; this path accepts complete "
                "literal SQL only. Inline the value as a quoted literal.",
                "placeholder",
            )

    if next(root.find_all(exp.Into), None) is not None or root.args.get("into"):
        return _reject(
            original,
            "SELECT ... INTO creates or writes a table.",
            "select_into",
        )

    for node in root.walk():
        locks = node.args.get("locks") if hasattr(node, "args") else None
        if locks:
            return _reject(
                original,
                "FOR UPDATE / FOR SHARE takes row locks on the source database.",
                "row_locks",
            )

    if denied_functions:
        present = _collect_function_names(root)
        normalized = root.sql(dialect=dialect).lower()
        for denied in denied_functions:
            needle = denied.lower()
            if needle in present or re.search(
                rf"(?<![\w.]){re.escape(needle)}\s*\(", normalized
            ):
                return _reject(
                    original,
                    f"Function {denied!r} is denied because it can hold or stall a database session.",
                    "denied_function",
                )

    existing = _literal_limit(root)
    has_limit_node = root.args.get("limit") is not None
    if not has_limit_node:
        root = _apply_limit(root, max_rows, dialect)
        applied, source = max_rows, "injected"
    elif existing is None:
        root = _apply_limit(root, max_rows, dialect)
        applied, source = max_rows, "clamped"
    elif existing > max_rows:
        root = _apply_limit(root, max_rows, dialect)
        applied, source = max_rows, "clamped"
    else:
        applied, source = existing, "existing"

    warnings: list[str] = []
    safe_sql = root.sql(dialect=dialect, pretty=False)
    root_select = root if isinstance(root, exp.Select) else None
    if root_select is not None and _selects_star(root_select):
        warnings.append("Selects all columns; narrow the column list to reduce replica I/O.")
    # sqlglot stores the FROM clause under "from_" (it moved from "from"), so
    # check both keys - reading the wrong one silently disables this warning.
    has_from = bool(root_select.args.get("from_") or root_select.args.get("from")) if root_select else False
    if root_select is not None and has_from and not root_select.args.get("where"):
        warnings.append("No WHERE clause; this may scan an entire table on a production replica.")
    for join in root.find_all(exp.Join):
        if not join.args.get("on") and not join.args.get("using"):
            warnings.append("Join without ON/USING; risks a cartesian product.")
            break

    return GuardVerdict(
        ok=True,
        original_sql=original,
        sql=safe_sql,
        applied_limit=applied,
        limit_source=source,
        warnings=tuple(dict.fromkeys(warnings)),
    )
