"""Authentication against Superset 4.0.

Flask-AppBuilder provides the JWT endpoints:

* ``POST /api/v1/security/login``     -> access token (+ refresh token)
* ``POST /api/v1/security/refresh``   -> new access token, authorised by the refresh token
* ``GET  /api/v1/security/csrf_token/`` -> CSRF token, required for mutating calls

The service-account password lives only in the macOS Keychain and is read on
demand. The access token is cached at 0600 in the state directory so ordinary
agent calls do not re-authenticate on every invocation.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from tathya.config import Config
from tathya.errors import AuthError, UpstreamError
from tathya.keychain import account_for_password, get_secret

#: Refresh this many seconds before the token actually expires.
EXPIRY_SKEW_SECONDS = 90


def _jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature.

    Superset is the verifier; this only reads metadata locally to decide when to
    refresh and to report who the token belongs to. A malformed token yields {}.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _jwt_expiry(token: str) -> float | None:
    value = _jwt_claims(token).get("exp")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Session:
    base_url: str
    username: str
    access_token: str
    refresh_token: str | None = None
    access_expires_at: float | None = None

    def fresh_for(self, base_url: str, username: str) -> bool:
        if self.base_url != base_url or self.username != username:
            return False
        if not self.access_token:
            return False
        if self.access_expires_at is None:
            return True
        return time.time() < self.access_expires_at - EXPIRY_SKEW_SECONDS


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Session | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text())
            return Session(**raw)
        except Exception:
            return None

    def save(self, session: Session) -> None:
        self.path.write_text(json.dumps(session.__dict__))
        self.path.chmod(0o600)

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


class Authenticator:
    """Owns tokens. Talks to the raw httpx client to avoid recursing through
    :class:`~tathya.client.SupersetClient`, which asks *this* for credentials."""

    def __init__(self, config: Config, http: httpx.Client, store: SessionStore) -> None:
        self.config = config
        self.http = http
        self.store = store
        self._session: Session | None = store.load()
        self._csrf: str | None = None

    # ------------------------------------------------------------------ tokens
    def _password(self) -> str:
        username = self.config.instance.username
        secret = get_secret(account_for_password(username))
        if not secret:
            raise AuthError(
                f"No Keychain password stored for {username!r}.",
                fix="Run `tathya auth login` and type the password at the prompt.",
            )
        return secret

    def _post(self, path: str, *, json_body: dict | None = None, bearer: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            response = self.http.post(path, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AuthError(f"Timed out contacting {self.config.instance.base_url}") from exc
        except httpx.TransportError as exc:
            raise AuthError(
                f"Could not reach {self.config.instance.base_url}: {exc}",
                hint="Check the base_url, VPN access, and TLS settings.",
            ) from exc
        if response.status_code in (401, 403):
            raise AuthError(
                "Superset rejected the credentials.",
                status=response.status_code,
                path=path,
                hint="Re-run `tathya auth login`; confirm the provider (db vs ldap) is right.",
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"Auth endpoint {path} returned HTTP {response.status_code}.",
                status=response.status_code,
                body=response.text[:500],
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError(f"Auth endpoint {path} returned a non-JSON body.") from exc

    def login(self) -> Session:
        """Full username/password login. Requires the Keychain entry."""
        instance = self.config.instance
        payload = {
            "username": instance.username,
            "password": self._password(),
            "provider": instance.provider,
            "refresh": True,
        }
        data = self._post("/api/v1/security/login", json_body=payload)
        access = data.get("access_token")
        if not access:
            raise AuthError("Login succeeded but no access_token was returned.")
        session = Session(
            base_url=instance.base_url,
            username=instance.username,
            access_token=access,
            refresh_token=data.get("refresh_token"),
            access_expires_at=_jwt_expiry(access),
        )
        self.store.save(session)
        self._session = session
        self._csrf = None
        return session

    def _refresh(self, session: Session) -> Session | None:
        if not session.refresh_token:
            return None
        try:
            data = self._post("/api/v1/security/refresh", bearer=session.refresh_token)
        except (AuthError, UpstreamError):
            return None
        access = data.get("access_token")
        if not access:
            return None
        session.access_token = access
        session.access_expires_at = _jwt_expiry(access)
        self.store.save(session)
        self._session = session
        self._csrf = None
        return session

    def access_token(self, *, force: bool = False) -> str:
        instance = self.config.instance
        session = self._session
        if not force and session and session.fresh_for(instance.base_url, instance.username):
            return session.access_token
        if session and session.base_url == instance.base_url and session.username == instance.username:
            refreshed = self._refresh(session)
            if refreshed:
                return refreshed.access_token
        return self.login().access_token

    def invalidate(self) -> None:
        """Called after a 401 so the next call re-authenticates."""
        if self._session:
            self._session.access_expires_at = 0.0
        self._csrf = None

    # -------------------------------------------------------------------- csrf
    def csrf_token(self) -> str:
        """Fetch (and cache) a CSRF token.

        The session cookie set by this response is retained by the shared httpx
        cookie jar; Superset validates the cookie and the header together, so
        both must come from the same client.
        """
        if self._csrf:
            return self._csrf
        token = self.access_token()
        try:
            response = self.http.get(
                "/api/v1/security/csrf_token/",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TransportError as exc:
            raise UpstreamError(f"Could not fetch a CSRF token: {exc}") from exc
        if response.status_code == 401:
            self.invalidate()
            token = self.access_token(force=True)
            response = self.http.get(
                "/api/v1/security/csrf_token/",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"CSRF token request returned HTTP {response.status_code}.",
                status=response.status_code,
                body=response.text[:300],
            )
        value = response.json().get("result")
        if not value:
            raise UpstreamError("CSRF token response contained no `result`.")
        self._csrf = value
        return value

    def status(self) -> dict:
        session = self._session
        instance = self.config.instance
        has_password = bool(get_secret(account_for_password(instance.username)))
        expires_in = None
        if session and session.access_expires_at:
            expires_in = round(session.access_expires_at - time.time())
        subject = _jwt_claims(session.access_token).get("sub") if session else None
        return {
            "base_url": instance.base_url,
            "username": instance.username,
            "provider": instance.provider,
            "superset_user_id": subject,
            "password_in_keychain": has_password,
            "cached_session": bool(session),
            "session_matches_config": bool(
                session and session.base_url == instance.base_url
                and session.username == instance.username
            ),
            "access_token_expires_in_seconds": expires_in,
            "has_refresh_token": bool(session and session.refresh_token),
        }
