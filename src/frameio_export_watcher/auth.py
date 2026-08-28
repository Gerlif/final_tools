"""Access tokens for the Frame.io V4 API.

Two modes are supported:

* ``ims``    -- Adobe IMS server-to-server OAuth (``client_credentials``).
                This is the forward-looking option; tokens live about an hour
                and are refreshed automatically.
* ``legacy`` -- a legacy Frame.io developer token. Only works for accounts that
                are not yet administered through the Adobe Admin Console, and
                requires the ``x-frameio-legacy-token-auth`` header. The legacy
                API is retired after 2026-12-01.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from .config import AuthConfig

log = logging.getLogger(__name__)

# Refresh a little before the token actually dies so long uploads do not start
# with a token that expires mid-flight.
_EXPIRY_MARGIN_SECONDS = 300.0


class AuthError(RuntimeError):
    """Raised when a token cannot be obtained."""


class TokenProvider:
    """Base class: supplies a bearer token and any extra auth headers."""

    extra_headers: dict[str, str] = {}

    def token(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def invalidate(self) -> None:
        """Drop any cached token; called after a 401 from the API."""


class LegacyTokenProvider(TokenProvider):
    """A static developer token."""

    extra_headers = {"x-frameio-legacy-token-auth": "true"}

    def __init__(self, token: str) -> None:
        self._token = token

    def token(self) -> str:
        return self._token


class ImsTokenProvider(TokenProvider):
    """Adobe IMS server-to-server client credentials flow."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scope: str,
        token_url: str,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._token_url = token_url
        self._session = session or requests.Session()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            self._token, self._expires_at = self._fetch()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> tuple[str, float]:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }
        try:
            response = self._session.post(
                self._token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise AuthError(f"could not reach Adobe IMS: {exc}") from exc

        if response.status_code >= 400:
            raise AuthError(
                f"Adobe IMS rejected the credentials (HTTP {response.status_code}): "
                f"{response.text[:400]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AuthError("Adobe IMS returned a non-JSON response") from exc

        token = body.get("access_token")
        if not token:
            raise AuthError("Adobe IMS response did not contain an access_token")
        lifetime = float(body.get("expires_in", 3600))
        expires_at = time.time() + max(60.0, lifetime - _EXPIRY_MARGIN_SECONDS)
        log.info("obtained Adobe IMS access token, valid for %.0f s", lifetime)
        return token, expires_at


def build_token_provider(
    config: AuthConfig, session: requests.Session | None = None
) -> TokenProvider:
    """Create the token provider described by the auth configuration."""
    if config.mode == "legacy":
        assert config.legacy_token  # validated in load_config
        log.warning(
            "using a legacy Frame.io developer token; the legacy API is retired "
            "after 2026-12-01 -- plan a move to Adobe IMS server-to-server auth"
        )
        return LegacyTokenProvider(config.legacy_token)

    assert config.client_id and config.client_secret  # validated in load_config
    return ImsTokenProvider(
        client_id=config.client_id,
        client_secret=config.client_secret,
        scope=config.scope,
        token_url=config.token_url,
        session=session,
    )
