"""Secret scrubbing.

Two independent layers, because either alone has a failure mode:

1. **Key-name matching** catches values whose key says they are secret. Matching
   is *exact or suffix-based*, not a loose substring test. A substring test was
   the original implementation and it masked `tokens_available` (rate-limit
   budget), `password_in_keychain` (a status flag), and
   `access_token_expires_in_seconds` (a diagnostic) - destroying useful output
   while protecting nothing.

2. **Value scanning** catches credentials wherever they appear, regardless of
   key name - DSN passwords and JWTs in free text, error bodies, and URLs. This
   is the layer that actually protects against an unanticipated field name.

Only strings can carry a secret here: every credential this tool touches (JWTs,
passwords, DSNs, webhook URLs) is a string, so numbers, booleans, and None under
a secret-looking key are metadata *about* a secret, not the secret.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***REDACTED***"

#: Key names whose value IS the secret. Matched exactly (case-insensitive).
SECRET_KEY_EXACT = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "csrf_token",
        "api_key",
        "apikey",
        "private_key",
        "credential",
        "credentials",
        "webhook",
        "webhook_url",
        "authorization",
        "cookie",
        "set_cookie",
    }
)

#: Compound names ending in one of these are secrets too, e.g. ``db_password``.
SECRET_KEY_SUFFIXES = (
    "_password",
    "_passwd",
    "_secret",
    "_api_key",
    "_apikey",
    "_private_key",
    "_webhook",
    "_webhook_url",
    "_credential",
    "_credentials",
    "_access_token",
    "_refresh_token",
)

#: Fragments that are credential-bearing wherever they appear in a key name.
SECRET_KEY_CONTAINS = ("sqlalchemy_uri", "encrypted_extra")

#: user:password@host in any DSN or URL form.
_DSN_RE = re.compile(r"(?P<scheme>[a-zA-Z0-9+.\-]+://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")

#: JWTs, which appear in Authorization headers and login responses.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b")


def _is_secret_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in SECRET_KEY_EXACT:
        return True
    if any(lowered.endswith(suffix) for suffix in SECRET_KEY_SUFFIXES):
        return True
    return any(fragment in lowered for fragment in SECRET_KEY_CONTAINS)


def _can_hold_a_secret(value: Any) -> bool:
    """Whether a value under a secret-looking key could actually carry a secret.

    Numbers, booleans, and None cannot: they are metadata about a secret, such as
    whether one is stored or when it expires. Containers can, so they are masked
    wholesale rather than walked.
    """
    return isinstance(value, (str, bytes, dict, list, tuple))


def redact_text(value: str) -> str:
    """Scrub secrets appearing inline in free text (error bodies, SQL, URLs)."""
    value = _DSN_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{MASK}@", value)
    return _JWT_RE.sub(MASK, value)


def redact(obj: Any) -> Any:
    """Recursively copy ``obj``, masking secrets by key name and by value."""
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if isinstance(key, str) and _is_secret_key(key) and _can_hold_a_secret(val):
                out[key] = MASK
            else:
                out[key] = redact(val)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
