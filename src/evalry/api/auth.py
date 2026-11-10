"""OIDC bearer-token verification. The tenant comes from a verified claim, never from the request body or path.

Algorithms are an explicit allow-list (asymmetric only), so "none" and HMAC-with-the-public-key tokens are rejected. Keys come
from a ``KeyResolver``: ``JwksKeys`` fetches the issuer's JWKS in production, ``StaticKeys`` serves a fixed key for tests and
local development. shortcut: no token revocation or introspection; tokens are trusted until they expire, so keep lifetimes short.
"""

from typing import Any, Protocol

import jwt

from evalry.access import Principal

ALGORITHMS = ("RS256", "ES256")


class AuthError(Exception):
    """The token is missing, malformed, expired or not meant for this service. Maps to HTTP 401."""


class KeyResolver(Protocol):
    def signing_key(self, token: str) -> Any: ...


class StaticKeys:
    def __init__(self, public_key: Any) -> None:
        self._key = public_key

    def signing_key(self, token: str) -> Any:
        return self._key


class JwksKeys:
    """Production resolver: the issuer's published keys, cached by PyJWT's client."""

    def __init__(self, jwks_url: str) -> None:
        if not jwks_url.startswith("https://"):
            raise ValueError("JWKS must be fetched over https. Pass the issuer's https://.../jwks.json URL.")
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=True, timeout=5)

    def signing_key(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


class TokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        keys: KeyResolver,
        tenant_claim: str = "tenant_id",
        leeway_s: int = 30,
    ) -> None:
        self._issuer, self._audience, self._keys = issuer, audience, keys
        self._tenant_claim, self._leeway = tenant_claim, leeway_s

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._keys.signing_key(token),
                algorithms=list(ALGORITHMS),
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, ValueError, OSError) as e:
            # One generic message: detail would help an attacker tune a forged token, and the cause goes to the log.
            raise AuthError("The access token is missing, invalid or expired. Sign in again.") from e
        subject, tenant = claims["sub"], claims.get(self._tenant_claim)
        if not isinstance(subject, str) or not subject or not isinstance(tenant, str) or not tenant:
            raise AuthError(
                f"The access token has no usable subject or {self._tenant_claim!r} claim. Contact your admin."
            )
        return Principal(subject=subject, tenant_id=tenant)
