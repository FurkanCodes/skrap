from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
import time

import httpx
import jwt
from app.core.perf import log_span


class AuthenticationError(RuntimeError):
    """Raised when bearer token validation fails."""

    def __init__(self, message: str, *, reason: str = "invalid_token") -> None:
        super().__init__(message)
        self.reason = reason


_logger = logging.getLogger("scraper_api.auth")


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
        algorithms: tuple[str, ...] = ("EdDSA",),
        jwks_timeout_seconds: float = 2.0,
    ) -> None:
        self._jwks_url = jwks_url.strip()
        self._issuer = issuer.strip()
        self._audience = audience.strip()
        self._algorithms = tuple(
            algorithm.strip() for algorithm in algorithms if algorithm.strip()
        )
        if not self._algorithms:
            raise ValueError("At least one JWT algorithm must be configured")
        self._cache_ttl = max(30, jwks_cache_ttl_seconds)
        self._jwks_timeout = max(0.1, jwks_timeout_seconds)
        self._lock = RLock()
        self._cached_set: jwt.PyJWKSet | None = None
        self._cache_expires_at: datetime | None = None

    def warm(self) -> None:
        """Prefetch JWKS so first authenticated requests do not pay cold latency."""
        with self._lock:
            if self._cached_set is not None and not self._is_cache_expired():
                return
            self._refresh_jwk_set(reason="warm")

    def verify(self, token: str) -> AuthPrincipal:
        verify_started = time.perf_counter()
        normalized = token.strip()
        if not normalized:
            raise AuthenticationError("Missing bearer token", reason="missing_token")

        try:
            header = jwt.get_unverified_header(normalized)
        except Exception as exc:
            raise AuthenticationError(
                f"Invalid token header: {exc}",
                reason="invalid_header",
            ) from exc

        kid = str(header.get("kid", "")).strip()
        if not kid:
            raise AuthenticationError("Token header missing kid", reason="missing_kid")

        algorithm = str(header.get("alg", "")).strip()
        if not algorithm:
            raise AuthenticationError(
                "Token header missing alg",
                reason="missing_algorithm",
            )
        if algorithm not in self._algorithms:
            raise AuthenticationError(
                "Token header alg is not allowed",
                reason="disallowed_algorithm",
            )

        signing_key = self._get_signing_key_for_kid(kid=kid)
        decode_started = time.perf_counter()
        try:
            claims = jwt.decode(
                normalized,
                key=signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "exp", "iss", "aud"],
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(
                "Invalid token claims: expired",
                reason="expired_token",
            ) from exc
        except Exception as exc:
            raise AuthenticationError(
                f"Invalid token claims: {exc}",
                reason="invalid_claims",
            ) from exc
        finally:
            log_span(
                component="auth",
                operation="jwt_decode",
                duration_ms=(time.perf_counter() - decode_started) * 1000,
            )

        audience = claims.get("aud")
        if isinstance(audience, list):
            audience_str = ",".join(str(value) for value in audience)
        else:
            audience_str = str(audience or "")

        principal = AuthPrincipal(
            subject=str(claims.get("sub", "")).strip(),
            email=_as_optional_str(claims.get("email")),
            name=_as_optional_str(claims.get("name")),
            issuer=str(claims.get("iss", "")).strip(),
            audience=audience_str,
        )
        log_span(
            component="auth",
            operation="verify",
            duration_ms=(time.perf_counter() - verify_started) * 1000,
        )
        return principal

    def _get_signing_key_for_kid(self, *, kid: str) -> jwt.PyJWK:
        with self._lock:
            jwk_set = self._cached_set
            if jwk_set is None or self._is_cache_expired():
                jwk_set = self._refresh_jwk_set(reason="miss_or_expired")

            match = _find_jwk(jwk_set, kid)
            if match is not None:
                return match

            # Refresh once on key miss to handle rotation.
            jwk_set = self._refresh_jwk_set(reason="unknown_kid")
            refreshed = _find_jwk(jwk_set, kid)
            if refreshed is None:
                raise AuthenticationError(
                    "Unknown signing key (kid)",
                    reason="unknown_kid",
                )
            return refreshed

    def _is_cache_expired(self) -> bool:
        if self._cache_expires_at is None:
            return True
        return datetime.now(tz=UTC) >= self._cache_expires_at

    def _refresh_jwk_set(self, *, reason: str) -> jwt.PyJWKSet:
        started = time.perf_counter()
        try:
            response = httpx.get(self._jwks_url, timeout=self._jwks_timeout)
            response.raise_for_status()
        except Exception as exc:
            raise AuthenticationError(
                f"JWKS fetch failed: {exc}",
                reason="jwks_fetch_failed",
            ) from exc

        payload = _parse_json_object(response.text)
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            raise AuthenticationError(
                "JWKS response missing keys",
                reason="jwks_missing_keys",
            )

        try:
            parsed = jwt.PyJWKSet.from_dict(payload)
        except Exception as exc:
            raise AuthenticationError(
                f"JWKS parse failed: {exc}",
                reason="jwks_parse_failed",
            ) from exc

        self._cached_set = parsed
        self._cache_expires_at = datetime.now(tz=UTC) + timedelta(seconds=self._cache_ttl)
        elapsed_ms = (time.perf_counter() - started) * 1000
        _logger.info(
            "jwks_cache_refreshed reason=%s duration_ms=%.2f",
            reason,
            elapsed_ms,
        )
        log_span(
            component="auth",
            operation="jwks_fetch",
            duration_ms=elapsed_ms,
            always=True,
        )
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
