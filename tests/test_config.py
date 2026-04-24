from __future__ import annotations

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_production_rejects_http_jwks_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("BETTER_AUTH_JWKS_URL", "http://auth.example.com/api/auth/jwks")
    monkeypatch.setenv("BETTER_AUTH_JWT_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("BETTER_AUTH_JWT_AUDIENCE", "https://api.example.com")

    with pytest.raises(RuntimeError, match="BETTER_AUTH_JWKS_URL must use https"):
        get_settings()


def test_production_rejects_http_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("BETTER_AUTH_JWKS_URL", "https://auth.example.com/api/auth/jwks")
    monkeypatch.setenv("BETTER_AUTH_JWT_ISSUER", "http://auth.example.com")
    monkeypatch.setenv("BETTER_AUTH_JWT_AUDIENCE", "https://api.example.com")

    with pytest.raises(RuntimeError, match="BETTER_AUTH_JWT_ISSUER must use https"):
        get_settings()


def test_localhost_http_auth_urls_are_allowed_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("BETTER_AUTH_JWKS_URL", "http://localhost:3000/api/auth/jwks")
    monkeypatch.setenv("BETTER_AUTH_JWT_ISSUER", "http://127.0.0.1:3000")
    monkeypatch.setenv("BETTER_AUTH_JWT_AUDIENCE", "http://127.0.0.1:8000")

    settings = get_settings()

    assert settings.better_auth_jwks_url == "http://localhost:3000/api/auth/jwks"
    assert settings.better_auth_jwt_issuer == "http://127.0.0.1:3000"
    assert settings.better_auth_jwt_algorithms == ("EdDSA",)
    assert settings.better_auth_jwks_timeout_seconds == 2.0
    assert settings.rate_limit_enabled is False
    assert settings.rate_limit_fail_open is True
    assert settings.server_fetch_allowed_ports == (80, 443)


def test_production_rejects_wildcard_cors_with_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")

    with pytest.raises(RuntimeError, match="CORS_ALLOW_ORIGINS must be explicit"):
        get_settings()


def test_production_enables_rate_limit_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("SCHEDULER_QUEUE_REDIS_URL", "redis://redis:6379/0")

    settings = get_settings()

    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_redis_url == "redis://redis:6379/0"
    assert settings.rate_limit_fail_open is False
    assert settings.server_fetch_resolve_dns is True
