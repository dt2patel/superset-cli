"""Error types and exit codes.

Exit codes are part of the agent-facing contract. An agent can branch on the
code without parsing prose, which is what makes automated retry decisions safe:
EXIT_LIMIT and EXIT_GUARD must never be retried, EXIT_UPSTREAM may be.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_GUARD = 5
EXIT_LIMIT = 6
EXIT_UPSTREAM = 7
EXIT_WRITE_BLOCKED = 8
EXIT_ALERT = 9
EXIT_TIMEOUT = 10

#: Codes an agent must treat as terminal - retrying makes things strictly worse.
NO_RETRY_CODES = frozenset(
    {EXIT_USAGE, EXIT_CONFIG, EXIT_GUARD, EXIT_LIMIT, EXIT_WRITE_BLOCKED}
)


class TathyaError(Exception):
    """Base for every deliberate failure the CLI reports."""

    exit_code = EXIT_ERROR
    kind = "error"
    retryable = False

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class UsageError(TathyaError):
    exit_code = EXIT_USAGE
    kind = "usage"


class ConfigError(TathyaError):
    exit_code = EXIT_CONFIG
    kind = "config"


class AuthError(TathyaError):
    exit_code = EXIT_AUTH
    kind = "auth"


class GuardError(TathyaError):
    """The SQL guard refused the statement. Never retry; rewrite the query."""

    exit_code = EXIT_GUARD
    kind = "guard_rejected"


class LimitError(TathyaError):
    """Rate limit, concurrency cap, or blackout window. Never retry in a loop."""

    exit_code = EXIT_LIMIT
    kind = "rate_limited"


class UpstreamError(TathyaError):
    """Superset returned an error. May be transient."""

    exit_code = EXIT_UPSTREAM
    kind = "upstream"
    retryable = True


class WriteBlocked(TathyaError):
    exit_code = EXIT_WRITE_BLOCKED
    kind = "write_blocked"


class QueryTimeout(TathyaError):
    exit_code = EXIT_TIMEOUT
    kind = "timeout"
