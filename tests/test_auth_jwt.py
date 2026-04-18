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


def _make_token_and_jwks(*, issuer: str, audience: str) -> tuple[str, dict[str, object]]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    kid = "test-kid-1"
    now = datetime.now(tz=UTC)
    token = jwt.encode(
        {
            "sub": "usr_123",
            "email": "user@example.com",
            "name": "Test User",
            "iss": issuer,
            "aud": audience,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="EdDSA",
        headers={"kid": kid},
    )
    jwks = {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "kid": kid,
                "alg": "EdDSA",
                "x": _base64url(public_raw),
            }
        ]
    }
    return token, jwks


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
