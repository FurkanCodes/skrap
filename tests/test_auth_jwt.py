from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.auth import AuthenticationError, BetterAuthJwtVerifier


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _make_token_and_jwks(
    *,
    issuer: str,
    audience: str,
    kid: str = "test-kid-1",
    expires_delta: timedelta = timedelta(minutes=5),
    algorithm: str = "EdDSA",
) -> tuple[str, dict[str, object]]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    now = datetime.now(tz=UTC)
    token = jwt.encode(
        {
            "sub": "usr_123",
            "email": "user@example.com",
            "name": "Test User",
            "iss": issuer,
            "aud": audience,
            "exp": now + expires_delta,
        },
        private_key,
        algorithm=algorithm,
        headers={"kid": kid},
    )
    jwks = {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "kid": kid,
                "alg": algorithm,
                "x": _base64url(public_raw),
            }
        ]
    }
    return token, jwks


def _replace_token_header(token: str, header: dict[str, object]) -> str:
    _old_header, payload, signature = token.split(".")
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    return ".".join([encoded_header, payload, signature])


def test_jwt_verifier_accepts_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    issuer = "https://ui.example.com"
    audience = "skrap-api"
    token, jwks = _make_token_and_jwks(issuer=issuer, audience=audience)

    monkeypatch.setattr("app.core.auth.httpx.get", lambda *_args, **_kwargs: _FakeResponse(jwks))

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer=issuer,
        audience=audience,
        jwks_cache_ttl_seconds=300,
    )
    principal = verifier.verify(token)

    assert principal.subject == "usr_123"
    assert principal.email == "user@example.com"
    assert principal.issuer == issuer
    assert principal.audience == audience


def test_jwt_verifier_rejects_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    issuer = "https://ui.example.com"
    token, jwks = _make_token_and_jwks(issuer=issuer, audience="skrap-api")

    monkeypatch.setattr("app.core.auth.httpx.get", lambda *_args, **_kwargs: _FakeResponse(jwks))

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer=issuer,
        audience="different-audience",
        jwks_cache_ttl_seconds=300,
    )

    with pytest.raises(AuthenticationError, match="Invalid token claims"):
        verifier.verify(token)


def test_jwt_verifier_rejects_wrong_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    token, jwks = _make_token_and_jwks(
        issuer="https://ui.example.com",
        audience="skrap-api",
    )

    monkeypatch.setattr("app.core.auth.httpx.get", lambda *_args, **_kwargs: _FakeResponse(jwks))

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer="https://other.example.com",
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
    )

    with pytest.raises(AuthenticationError, match="Invalid token claims") as exc:
        verifier.verify(token)

    assert exc.value.reason == "invalid_claims"


def test_jwt_verifier_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    issuer = "https://ui.example.com"
    token, jwks = _make_token_and_jwks(
        issuer=issuer,
        audience="skrap-api",
        expires_delta=timedelta(minutes=-5),
    )

    monkeypatch.setattr("app.core.auth.httpx.get", lambda *_args, **_kwargs: _FakeResponse(jwks))

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer=issuer,
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
    )

    with pytest.raises(AuthenticationError, match="expired") as exc:
        verifier.verify(token)

    assert exc.value.reason == "expired_token"


def test_jwt_verifier_rejects_missing_kid() -> None:
    token, _jwks = _make_token_and_jwks(
        issuer="https://ui.example.com",
        audience="skrap-api",
    )
    token = _replace_token_header(token, {"alg": "EdDSA"})

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer="https://ui.example.com",
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
    )

    with pytest.raises(AuthenticationError, match="missing kid") as exc:
        verifier.verify(token)

    assert exc.value.reason == "missing_kid"


def test_jwt_verifier_rejects_missing_alg() -> None:
    token, _jwks = _make_token_and_jwks(
        issuer="https://ui.example.com",
        audience="skrap-api",
    )
    token = _replace_token_header(token, {"kid": "test-kid-1"})

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer="https://ui.example.com",
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
    )

    with pytest.raises(AuthenticationError, match="missing alg") as exc:
        verifier.verify(token)

    assert exc.value.reason == "missing_algorithm"


def test_jwt_verifier_rejects_disallowed_alg() -> None:
    token, _jwks = _make_token_and_jwks(
        issuer="https://ui.example.com",
        audience="skrap-api",
    )
    token = _replace_token_header(token, {"kid": "test-kid-1", "alg": "RS256"})

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer="https://ui.example.com",
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
        algorithms=("EdDSA",),
    )

    with pytest.raises(AuthenticationError, match="not allowed") as exc:
        verifier.verify(token)

    assert exc.value.reason == "disallowed_algorithm"


def test_jwt_verifier_warm_fetches_once_and_verify_uses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "https://ui.example.com"
    token, jwks = _make_token_and_jwks(issuer=issuer, audience="skrap-api")
    calls = 0

    def fake_get(*_args, **_kwargs):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return _FakeResponse(jwks)

    monkeypatch.setattr("app.core.auth.httpx.get", fake_get)

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer=issuer,
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
    )

    verifier.warm()
    principal = verifier.verify(token)

    assert principal.subject == "usr_123"
    assert calls == 1


def test_jwt_verifier_unknown_kid_refreshes_once_after_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = "https://ui.example.com"
    _warm_token, warm_jwks = _make_token_and_jwks(
        issuer=issuer,
        audience="skrap-api",
        kid="known-kid",
    )
    unknown_token, _unknown_jwks = _make_token_and_jwks(
        issuer=issuer,
        audience="skrap-api",
        kid="unknown-kid",
    )
    calls = 0

    def fake_get(*_args, **_kwargs):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return _FakeResponse(warm_jwks)

    monkeypatch.setattr("app.core.auth.httpx.get", fake_get)

    verifier = BetterAuthJwtVerifier(
        jwks_url="https://ui.example.com/api/auth/jwks",
        issuer=issuer,
        audience="skrap-api",
        jwks_cache_ttl_seconds=300,
    )

    verifier.warm()
    with pytest.raises(AuthenticationError, match="Unknown signing key") as exc:
        verifier.verify(unknown_token)

    assert exc.value.reason == "unknown_kid"
    assert calls == 2
