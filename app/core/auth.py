from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

import httpx
import jwt


class AuthenticationError(RuntimeError):
    """Raised when bearer token validation fails."""


@dataclass(frozen=True)
class AuthPrincipal:
    """Identity claims extracted from a validated Better Auth JWT."""

    subject: str
    email: str | None
    name: str | None
    issuer: str
    audience: str
    provider: str = "better_auth"


class BetterAuthJwtVerifier:
    """Verifies Better Auth JWTs against JWKS with local cache."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        jwks_cache_ttl_seconds: int,
    ) -> None:
        self._jwks_url = jwks_url.strip()
        self._issuer = issuer.strip()
        self._audience = audience.strip()
        self._cache_ttl = max(30, jwks_cache_ttl_seconds)
        self._lock = RLock()
        self._cached_set: jwt.PyJWKSet | None = None
        self._cache_expires_at: datetime | None = None

    def verify(self, token: str) -> AuthPrincipal:
        normalized = token.strip()
        if not normalized:
            raise AuthenticationError("Missing bearer token")

        try:
            header = jwt.get_unverified_header(normalized)
        except Exception as exc:
            raise AuthenticationError(f"Invalid token header: {exc}") from exc

        kid = str(header.get("kid", "")).strip()
        if not kid:
            raise AuthenticationError("Token header missing kid")

        signing_key = self._get_signing_key_for_kid(kid=kid)
        try:
            claims = jwt.decode(
                normalized,
                key=signing_key.key,
                algorithms=[str(header.get("alg", "RS256"))],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "exp", "iss", "aud"],
                },
            )
        except Exception as exc:
            raise AuthenticationError(f"Invalid token claims: {exc}") from exc

        audience = claims.get("aud")
        if isinstance(audience, list):
            audience_str = ",".join(str(value) for value in audience)
        else:
            audience_str = str(audience or "")

        return AuthPrincipal(
            subject=str(claims.get("sub", "")).strip(),
            email=_as_optional_str(claims.get("email")),
            name=_as_optional_str(claims.get("name")),
            issuer=str(claims.get("iss", "")).strip(),
            audience=audience_str,
        )

    def _get_signing_key_for_kid(self, *, kid: str) -> jwt.PyJWK:
        with self._lock:
            jwk_set = self._cached_set
            if jwk_set is None or self._is_cache_expired():
                jwk_set = self._refresh_jwk_set()

            match = _find_jwk(jwk_set, kid)
            if match is not None:
                return match

            # Refresh once on key miss to handle rotation.
            jwk_set = self._refresh_jwk_set()
            refreshed = _find_jwk(jwk_set, kid)
            if refreshed is None:
                raise AuthenticationError("Unknown signing key (kid)")
            return refreshed

    def _is_cache_expired(self) -> bool:
        if self._cache_expires_at is None:
            return True
        return datetime.now(tz=UTC) >= self._cache_expires_at

    def _refresh_jwk_set(self) -> jwt.PyJWKSet:
        try:
            response = httpx.get(self._jwks_url, timeout=10)
            response.raise_for_status()
        except Exception as exc:
            raise AuthenticationError(f"JWKS fetch failed: {exc}") from exc

        payload = _parse_json_object(response.text)
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            raise AuthenticationError("JWKS response missing keys")

        try:
            parsed = jwt.PyJWKSet.from_dict(payload)
        except Exception as exc:
            raise AuthenticationError(f"JWKS parse failed: {exc}") from exc

        self._cached_set = parsed
        self._cache_expires_at = datetime.now(tz=UTC) + timedelta(seconds=self._cache_ttl)
        return parsed


def _find_jwk(jwk_set: jwt.PyJWKSet, kid: str) -> jwt.PyJWK | None:
    for jwk in jwk_set.keys:
        if str(jwk.key_id or "").strip() == kid:
            return jwk
    return None


def _parse_json_object(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise AuthenticationError(f"Invalid JWKS JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AuthenticationError("JWKS response must be a JSON object")
    return parsed


def _as_optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None

