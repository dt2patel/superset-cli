"""HTTP client for the Superset REST API.

The retry policy is a safety control, not a convenience: **mutating requests are
never retried automatically.** A retried ``POST /api/v1/sqllab/execute/`` runs
the query on the client replica a second time, which is precisely the load
pattern this tool exists to prevent. Idempotent GETs may retry with backoff.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

import httpx

from tathya import __version__
from tathya.auth import Authenticator, SessionStore
from tathya.config import Config, state_dir
from tathya.errors import AuthError, QueryTimeout, UpstreamError
from tathya.redact import redact_text

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
RETRYABLE_STATUS = {429, 502, 503, 504}


def _error_detail(response: httpx.Response) -> tuple[str, dict]:
    """Pull a usable message out of a Superset error body."""
    try:
        body = response.json()
    except ValueError:
        return redact_text(response.text[:400]) or f"HTTP {response.status_code}", {}
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = first.get("message") or json.dumps(first)[:300]
            extra = {
                key: first.get(key)
                for key in ("error_type", "level")
                if first.get(key) is not None
            }
            return redact_text(str(message)), extra
        if body.get("message") is not None:
            return redact_text(str(body["message"]))[:600], {}
    return redact_text(json.dumps(body, default=str)[:400]), {}


class SupersetClient:
    def __init__(self, config: Config, *, max_retries: int = 2) -> None:
        self.config = config
        self.max_retries = max_retries
        instance = config.instance
        self._http = httpx.Client(
            base_url=instance.base_url,
            verify=instance.verify_tls,
            timeout=httpx.Timeout(instance.timeout_seconds),
            follow_redirects=False,
            headers={
                "User-Agent": f"tathya-cli/{__version__}",
                "Accept": "application/json",
            },
        )
        self.auth = Authenticator(config, self._http, SessionStore(state_dir() / "session.json"))

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SupersetClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        allow_retry: bool | None = None,
        expect_json: bool = True,
    ) -> Any:
        method = method.upper()
        mutating = method in MUTATING_METHODS
        if allow_retry is None:
            allow_retry = not mutating  # never auto-retry a write or a query

        attempt = 0
        reauthenticated = False
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self.auth.access_token()}"}
            if mutating:
                headers["X-CSRFToken"] = self.auth.csrf_token()
                headers["Referer"] = f"{self.config.instance.base_url}/"
                if json_body is not None:
                    headers["Content-Type"] = "application/json"
            try:
                response = self._http.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=headers,
                    timeout=timeout if timeout is not None else self._http.timeout,
                )
            except httpx.TimeoutException as exc:
                raise QueryTimeout(
                    f"{method} {path} timed out.",
                    hint="The query may still be running on the database; do not immediately retry.",
                ) from exc
            except httpx.TransportError as exc:
                if allow_retry and attempt <= self.max_retries:
                    continue
                raise UpstreamError(f"{method} {path} failed: {exc}") from exc

            if response.status_code == 401 and not reauthenticated:
                self.auth.invalidate()
                self.auth.access_token(force=True)
                reauthenticated = True
                continue

            if response.status_code in (301, 302, 303, 307, 308):
                raise AuthError(
                    f"{method} {path} was redirected to {response.headers.get('location','?')}, "
                    "which usually means the session was not accepted.",
                    status=response.status_code,
                )

            if response.status_code in RETRYABLE_STATUS and allow_retry and attempt <= self.max_retries:
                continue

            if response.status_code == 403:
                message, extra = _error_detail(response)
                raise AuthError(
                    f"Permission denied by Superset: {message}",
                    status=403,
                    path=path,
                    **extra,
                )
            if response.status_code >= 400:
                message, extra = _error_detail(response)
                raise UpstreamError(
                    f"{method} {path} -> HTTP {response.status_code}: {message}",
                    status=response.status_code,
                    retryable=response.status_code in RETRYABLE_STATUS,
                    **extra,
                )

            if not expect_json:
                return response.content
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise UpstreamError(
                    f"{method} {path} returned a non-JSON body (HTTP {response.status_code}).",
                    body=redact_text(response.text[:300]),
                ) from exc

    # ------------------------------------------------------------- shortcuts
    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def list_all(
        self,
        path: str,
        *,
        q_builder,
        page_size: int = 100,
        max_pages: int = 20,
    ) -> tuple[list[dict], int]:
        """Page through a Superset list endpoint.

        ``max_pages`` is a deliberate stop: these endpoints are metadata-only, but
        an unbounded loop against a large instance is still a bad default.
        """
        results: list[dict] = []
        total = 0
        for page in range(max_pages):
            payload = self.get(path, params={"q": q_builder(page, page_size)})
            total = payload.get("count", total)
            chunk = payload.get("result") or []
            results.extend(chunk)
            if len(results) >= total or not chunk:
                break
        return results, total
