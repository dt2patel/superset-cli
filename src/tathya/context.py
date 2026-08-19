"""Shared per-invocation state and the write gate."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from tathya.audit import AuditLog
from tathya.client import SupersetClient
from tathya.config import Config, state_dir
from tathya.errors import WriteBlocked
from tathya.limiter import Limiter
from tathya.output import Emitter

WRITE_ENV_VAR = "TATHYA_ALLOW_WRITE"


@dataclass
class Context:
    config: Config
    emitter: Emitter
    args: Any
    _client: SupersetClient | None = field(default=None, repr=False)
    _limiter: Limiter | None = field(default=None, repr=False)
    _audit: AuditLog | None = field(default=None, repr=False)

    @property
    def client(self) -> SupersetClient:
        if self._client is None:
            self._client = SupersetClient(self.config)
        return self._client

    @property
    def limiter(self) -> Limiter:
        if self._limiter is None:
            self._limiter = Limiter(self.config.safety, state_dir())
        return self._limiter

    @property
    def audit(self) -> AuditLog:
        if self._audit is None:
            self._audit = AuditLog(state_dir() / "audit.jsonl")
        return self._audit

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # ------------------------------------------------------------- write gate
    @property
    def confirmed(self) -> bool:
        return bool(getattr(self.args, "confirm", False))

    def check_write(self, action: str, *, destructive: bool = False) -> None:
        """Gate an actual write. Callers must only reach here when confirmed.

        Two independent switches must both be on, so neither a stray ``--confirm``
        in a prompt nor an exported env var is sufficient alone.
        """
        if os.environ.get(WRITE_ENV_VAR) != "1":
            raise WriteBlocked(
                f"Writes are disabled, so {action!r} was not performed.",
                fix=f"Set {WRITE_ENV_VAR}=1 in the environment to enable writes.",
                note="This is separate from --confirm on purpose; both are required.",
            )
        if destructive and not self.config.safety.allow_delete:
            raise WriteBlocked(
                f"{action!r} deletes data and safety.allow_delete is false.",
                fix="Set allow_delete = true under [safety] if this is really intended.",
            )
