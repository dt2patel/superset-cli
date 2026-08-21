"""Configuration loading and the database allowlist.

Deny-by-default is the core rule: a database Tathya knows about is *not*
queryable by an agent until it is listed under ``[[databases]]`` here. Tathya
points at per-client production OMS replicas, so "which databases may an agent
touch" is a decision that belongs in a reviewed file, not in a prompt.
"""

from __future__ import annotations

import os
import tomllib
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path

from tathya.errors import ConfigError

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "tathya" / "config.toml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "tathya"


def config_path() -> Path:
    return Path(os.environ.get("TATHYA_CONFIG", DEFAULT_CONFIG_PATH)).expanduser()


def state_dir() -> Path:
    path = Path(os.environ.get("TATHYA_STATE_DIR", DEFAULT_STATE_DIR)).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def watches_dir() -> Path:
    override = os.environ.get("TATHYA_WATCHES_DIR")
    if override:
        return Path(override).expanduser()
    return config_path().parent / "watches"


#: Values shipped in the starter template. A config still carrying them has not
#: been filled in, and every downstream error would be a confusing symptom of
#: that rather than the cause.
PLACEHOLDER_USERNAMES = frozenset({"REPLACE-ME", "replace-me", ""})
PLACEHOLDER_BASE_URLS = frozenset({"https://superset.example.com", "https://REPLACE-ME.example.com"})


def normalize_base_url(raw: str) -> tuple[str, str | None]:
    """Reduce a pasted URL to its origin.

    People paste the page they were looking at - ``https://superset.example.com/
    superset/welcome/`` - and every ``/api/v1/...`` call then resolves under that
    path and 404s. Returns (origin, discarded) so the caller can say what it
    dropped instead of silently changing the input.
    """
    text = (raw or "").strip()
    if not text:
        return "", None
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    parts = urlsplit(text)
    if not parts.netloc:
        return text.rstrip("/"), None
    origin = f"{parts.scheme}://{parts.netloc}"
    discarded = "".join(
        part for part in (parts.path.rstrip("/"), f"?{parts.query}" if parts.query else "") if part
    )
    return origin, (discarded or None)


@dataclass(frozen=True)
class InstanceConfig:
    base_url: str
    username: str
    provider: str = "db"
    verify_tls: bool = True
    timeout_seconds: float = 60.0


#: Superset `backend` -> sqlglot dialect. Superset's names differ from
#: sqlglot's (postgresql/mssql vs postgres/tsql).
BACKEND_TO_DIALECT = {
    "mysql": "mysql",
    "postgresql": "postgres",
    "postgres": "postgres",
    "mssql": "tsql",
    "sqlite": "sqlite",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "trino": "trino",
    "presto": "presto",
}


@dataclass(frozen=True)
class DatabaseRule:
    """One allowlisted database. ``max_rows`` overrides the global cap downward."""

    name: str
    id: int | None = None
    default_schema: str | None = None
    max_rows: int | None = None
    #: sqlglot dialect used to parse SQL for this database. Without it, valid
    #: dialect-specific syntax is rejected as unparseable - MySQL backtick
    #: identifiers being the common case.
    dialect: str | None = None
    note: str = ""


@dataclass(frozen=True)
class SafetyConfig:
    max_rows: int = 1000
    hard_max_rows: int = 10_000
    query_timeout_seconds: float = 45.0
    max_queries_per_minute: int = 6
    max_concurrent_queries: int = 1
    max_queries_per_day: int = 300
    require_estimate: bool = False
    allow_delete: bool = False
    denied_functions: tuple[str, ...] = (
        "sleep",
        "pg_sleep",
        "benchmark",
        "waitfor",
        "dbms_pipe.receive_message",
    )
    blackout_windows: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportingConfig:
    default_targets: tuple[str, ...] = ("local",)
    gchat_space_label: str = ""
    gchat_thread_key: str = ""


@dataclass(frozen=True)
class DigestConfig:
    """Thresholds that decide what the digest flags for attention."""

    window: str = "6h"
    slow_query_seconds: float = 30.0
    failure_rate_pct: float = 10.0
    top_databases: int = 8


@dataclass(frozen=True)
class Config:
    instance: InstanceConfig
    #: Non-fatal problems found while loading, surfaced by health and setup.
    warnings: tuple[str, ...] = ()
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    databases: tuple[DatabaseRule, ...] = ()
    source_path: Path | None = None

    def database_rule(self, ref: str | int) -> DatabaseRule:
        """Resolve a database by name or id, refusing anything not allowlisted."""
        if not self.databases:
            raise ConfigError(
                "No databases are allowlisted, so no query may run. "
                "Add a [[databases]] entry to the config file.",
                config=str(self.source_path),
            )
        text = str(ref).strip()
        for rule in self.databases:
            if rule.name.lower() == text.lower():
                return rule
            if rule.id is not None and text.isdigit() and rule.id == int(text):
                return rule
        raise ConfigError(
            f"Database {text!r} is not allowlisted for agent access.",
            allowed=[rule.name for rule in self.databases],
            config=str(self.source_path),
        )

    def row_cap_for(self, rule: DatabaseRule) -> int:
        """Effective row cap: the tighter of the global and per-database limits."""
        cap = self.safety.max_rows
        if rule.max_rows is not None:
            cap = min(cap, rule.max_rows)
        return max(1, min(cap, self.safety.hard_max_rows))


def _require(table: dict, key: str, where: str) -> object:
    if key not in table or table[key] in (None, ""):
        raise ConfigError(f"Missing required config value {where}.{key}")
    return table[key]


def load(path: Path | None = None) -> Config:
    resolved = path or config_path()
    if not resolved.exists():
        raise ConfigError(
            f"No config at {resolved}. Run `tathya init` to create one.",
            path=str(resolved),
        )
    try:
        raw = tomllib.loads(resolved.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config at {resolved} is not valid TOML: {exc}") from exc

    inst_raw = raw.get("instance") or {}
    warnings: list[str] = []
    raw_base = str(_require(inst_raw, "base_url", "instance"))
    if raw_base.strip() in PLACEHOLDER_BASE_URLS:
        raise ConfigError(
            "instance.base_url is still the starter placeholder.",
            fix="Run `tathya setup` to configure this instance interactively.",
            config=str(resolved),
        )
    base_url, discarded = normalize_base_url(raw_base)
    if not base_url:
        raise ConfigError("instance.base_url is empty")
    if discarded:
        warnings.append(
            f"instance.base_url contained the path {discarded!r}, which was ignored - "
            f"using the origin {base_url}. Set base_url to the origin only."
        )
    provider = str(inst_raw.get("provider", "db")).lower()
    # Flask-AppBuilder's JWT login implements only these two branches; an OAuth/SSO
    # account cannot obtain a token from /api/v1/security/login at all.
    if provider not in {"db", "ldap"}:
        raise ConfigError(
            f"instance.provider must be 'db' or 'ldap', not {provider!r}.",
            note="Superset's API login has no OAuth/SSO branch.",
        )
    username = str(_require(inst_raw, "username", "instance"))
    if username.strip() in PLACEHOLDER_USERNAMES:
        raise ConfigError(
            "instance.username is still the starter placeholder.",
            fix="Run `tathya setup` to configure this instance interactively.",
            config=str(resolved),
        )
    instance = InstanceConfig(
        base_url=base_url,
        username=username,
        provider=provider,
        verify_tls=bool(inst_raw.get("verify_tls", True)),
        timeout_seconds=float(inst_raw.get("timeout_seconds", 60.0)),
    )

    safety_raw = dict(raw.get("safety") or {})
    blackout = tuple(str(w) for w in safety_raw.pop("blackout_windows", ()) or ())
    denied = safety_raw.pop("denied_functions", None)
    defaults = SafetyConfig()
    known = {f for f in SafetyConfig.__dataclass_fields__ if f not in {"blackout_windows", "denied_functions"}}
    unknown = set(safety_raw) - known
    if unknown:
        raise ConfigError(f"Unknown [safety] keys: {sorted(unknown)}")
    safety = SafetyConfig(
        blackout_windows=blackout,
        denied_functions=tuple(str(f).lower() for f in denied) if denied else defaults.denied_functions,
        **{k: type(getattr(defaults, k))(v) for k, v in safety_raw.items()},
    )
    if safety.max_rows > safety.hard_max_rows:
        raise ConfigError("safety.max_rows cannot exceed safety.hard_max_rows")

    rep_raw = raw.get("reporting") or {}
    reporting = ReportingConfig(
        default_targets=tuple(str(t) for t in rep_raw.get("default_targets", ("local",))),
        gchat_space_label=str(rep_raw.get("gchat_space_label", "")),
        gchat_thread_key=str(rep_raw.get("gchat_thread_key", "")),
    )

    digest_raw = raw.get("digest") or {}
    digest = DigestConfig(
        window=str(digest_raw.get("window", "6h")),
        slow_query_seconds=float(digest_raw.get("slow_query_seconds", 30.0)),
        failure_rate_pct=float(digest_raw.get("failure_rate_pct", 10.0)),
        top_databases=int(digest_raw.get("top_databases", 8)),
    )

    databases: list[DatabaseRule] = []
    for entry in raw.get("databases") or []:
        if "name" not in entry:
            raise ConfigError("Every [[databases]] entry needs a `name`")
        dialect = entry.get("dialect")
        if dialect is not None:
            import sqlglot

            # Enum *values* are the dialect names ("mysql"); str() on a member
            # yields "Dialects.MYSQL", which matches nothing.
            known = {
                member.value
                for member in sqlglot.dialects.Dialects.__members__.values()
                if member.value
            }
            if str(dialect).lower() not in known:
                raise ConfigError(
                    f"Unknown sqlglot dialect {dialect!r} for database {entry['name']!r}.",
                    hint="Common values: mysql, postgres, tsql, sqlite, trino, bigquery.",
                )
            dialect = str(dialect).lower()
        databases.append(
            DatabaseRule(
                name=str(entry["name"]),
                id=int(entry["id"]) if entry.get("id") is not None else None,
                default_schema=entry.get("default_schema"),
                max_rows=int(entry["max_rows"]) if entry.get("max_rows") is not None else None,
                dialect=dialect,
                note=str(entry.get("note", "")),
            )
        )

    return Config(
        warnings=tuple(warnings),
        instance=instance,
        safety=safety,
        reporting=reporting,
        digest=digest,
        databases=tuple(databases),
        source_path=resolved,
    )


DEFAULT_CONFIG_TOML = '''# tathya CLI configuration
# Secrets are NEVER stored here. The service-account password and the Google
# Chat webhook live in the macOS Keychain (`tathya auth login`, `tathya notify set-webhook`).

[instance]
base_url = "https://superset.example.com"
username = "REPLACE-ME"
provider = "db"          # "db" for Superset-local accounts, "ldap" for LDAP
verify_tls = true
timeout_seconds = 60

[safety]
# Row cap injected into every ad-hoc query that lacks its own LIMIT.
max_rows = 1000
# Ceiling no per-database override may exceed.
hard_max_rows = 10000
query_timeout_seconds = 45

# Rate limits exist because Tathya queries live client OMS read-replicas.
# Hand-run queries have pushed a replica to 100% CPU here; an agent
# retry loop is strictly worse. These caps are enforced locally, before the
# request is sent, and a breach is a terminal error rather than a wait.
max_queries_per_minute = 6
max_concurrent_queries = 1
max_queries_per_day = 300

# Ask the engine to cost the query before running it (engine-dependent).
require_estimate = false

# Deleting dashboards/charts stays off unless deliberately enabled.
allow_delete = false

# Time windows where ad-hoc SQL is refused outright. Local time.
# Example: protect OMS peak hours.
blackout_windows = []
# blackout_windows = ["Mon-Fri 09:00-11:00", "Mon-Fri 17:00-19:00"]

[reporting]
default_targets = ["local"]
gchat_space_label = ""

[digest]
# What `tathya digest` flags for attention. These read Superset's own query
# history, so tuning them costs nothing on any client database.
window = "6h"
slow_query_seconds = 30      # flag a database whose slowest query exceeds this
failure_rate_pct = 10        # flag a database failing more than this share
top_databases = 8

# ---------------------------------------------------------------------------
# Database allowlist - DENY BY DEFAULT.
# An agent cannot query a database that is not listed here, even if Tathya
# exposes it. Keep this list to the smallest set that answers real questions.
# ---------------------------------------------------------------------------
# [[databases]]
# name = "acme_oms"
# id = 3                 # optional; resolved by name if omitted
# dialect = "mysql"      # match the Superset backend, or MySQL backticks are
#                        # rejected as unparseable (postgresql -> "postgres",
#                        # mssql -> "tsql")
# default_schema = "public"
# max_rows = 500         # tighter than the global cap
# note = "Production OMS read-replica - keep queries indexed and narrow"
'''
