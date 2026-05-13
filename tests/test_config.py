from __future__ import annotations

from pathlib import Path

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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("BETTER_AUTH_JWKS_URL", "http://auth.example.com/api/auth/jwks")
    monkeypatch.setenv("BETTER_AUTH_JWT_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("BETTER_AUTH_JWT_AUDIENCE", "https://api.example.com")

    with pytest.raises(RuntimeError, match="BETTER_AUTH_JWKS_URL must use https"):
        get_settings()


def test_production_rejects_http_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
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
    monkeypatch.setenv("SCRAPE_PROXY_URLS", "")
    monkeypatch.setenv("SCRAPE_PROXY_LIST_URLS", "")

    settings = get_settings()

    assert settings.better_auth_jwks_url == "http://localhost:3000/api/auth/jwks"
    assert settings.better_auth_jwt_issuer == "http://127.0.0.1:3000"
    assert settings.better_auth_jwt_algorithms == ("EdDSA",)
    assert settings.better_auth_jwks_timeout_seconds == 2.0
    assert settings.rate_limit_enabled is False
    assert settings.rate_limit_fail_open is True
    assert settings.server_fetch_allowed_ports == (80, 443)
    assert settings.scrape_proxy_urls == ()


def test_scrape_proxy_urls_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPE_PROXY_URLS", "http://user:pass@proxy.example:8000, socks5://proxy2.example:1080")

    settings = get_settings()

    assert settings.scrape_proxy_urls == (
        "http://user:pass@proxy.example:8000",
        "socks5://proxy2.example:1080",
    )
    assert settings.scrape_proxy_list_urls == ()


def test_scrapling_dependency_pin_is_current() -> None:
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "scrapling[fetchers]==0.4.8" in requirements


def test_scrape_proxy_list_urls_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPE_PROXY_LIST_URLS", "https://proxy.example/list.txt")

    settings = get_settings()

    assert settings.scrape_proxy_list_urls == ("https://proxy.example/list.txt",)


def test_invalid_scrape_proxy_urls_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPE_PROXY_URLS", "ftp://proxy.example:21")

    with pytest.raises(RuntimeError, match="SCRAPE_PROXY_URLS must contain valid"):
        get_settings()


def test_invalid_scrape_proxy_list_urls_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPE_PROXY_LIST_URLS", "ftp://proxy.example/list.txt")

    with pytest.raises(RuntimeError, match="SCRAPE_PROXY_LIST_URLS must contain valid"):
        get_settings()


def test_production_rejects_wildcard_cors_with_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    with pytest.raises(RuntimeError, match="CORS_ALLOW_ORIGINS must be explicit"):
        get_settings()


def test_production_enables_rate_limit_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SCHEDULER_QUEUE_REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("SERVER_FETCH_RESOLVE_DNS", "")
    monkeypatch.setenv("BILLING_BACKEND_SECRET", "backend-secret")
    monkeypatch.setenv("PADDLE_PLUS_PRICE_ID", "pri_plus")
    monkeypatch.setenv("PADDLE_PRO_PRICE_ID", "pri_pro")

    settings = get_settings()

    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_redis_url == "redis://redis:6379/0"
    assert settings.rate_limit_fail_open is False
    assert settings.server_fetch_resolve_dns is True


def test_production_requires_paddle_price_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("BILLING_BACKEND_SECRET", "backend-secret")
    monkeypatch.setenv("PADDLE_PLUS_PRICE_ID", "")
    monkeypatch.setenv("PADDLE_PRO_PRICE_ID", "pri_pro")

    with pytest.raises(RuntimeError, match="PADDLE_PLUS_PRICE_ID is required"):
        get_settings()


def test_paddle_price_ids_must_be_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("PADDLE_PLUS_PRICE_ID", "pri_same")
    monkeypatch.setenv("PADDLE_PRO_PRICE_ID", "pri_same")

    with pytest.raises(RuntimeError, match="must be different"):
        get_settings()


def test_production_requires_telegram_webhook_secret_when_bot_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token-123")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")

    with pytest.raises(RuntimeError, match="TELEGRAM_WEBHOOK_SECRET is required"):
        get_settings()
