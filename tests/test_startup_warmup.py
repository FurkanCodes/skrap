from __future__ import annotations

import logging

import pytest

from app.api import dependencies
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("BETTER_AUTH_JWKS_URL", "http://localhost:3000/api/auth/jwks")
    monkeypatch.setenv("BETTER_AUTH_JWT_ISSUER", "http://localhost:3000")
    monkeypatch.setenv("BETTER_AUTH_JWT_AUDIENCE", "http://127.0.0.1:8000")
    monkeypatch.setenv("BETTER_AUTH_JWT_ALGORITHMS", "EdDSA")
    monkeypatch.setenv("BETTER_AUTH_JWKS_TIMEOUT_SECONDS", "2")


def test_warm_application_dependencies_calls_auth_and_db_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_auth_env(monkeypatch)
    calls: list[str] = []

    class _FakeVerifier:
        def warm(self) -> None:
            calls.append("jwt_verifier")

    monkeypatch.setattr(
        dependencies,
        "_get_jwt_verifier_cached",
        lambda **_kwargs: _FakeVerifier(),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_tracking_repository_cached",
        lambda **_kwargs: calls.append("tracking_repository"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_tracking_service_cached",
        lambda **_kwargs: calls.append("tracking_service"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_telegram_integration_service_cached",
        lambda **_kwargs: calls.append("telegram_service"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_user_service_cached",
        lambda **_kwargs: calls.append("user_service"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_billing_service_cached",
        lambda **_kwargs: calls.append("billing_service"),
    )

    dependencies.warm_application_dependencies(get_settings())

    assert calls == [
        "jwt_verifier",
        "tracking_repository",
        "tracking_service",
        "telegram_service",
        "user_service",
        "billing_service",
    ]


def test_warm_application_dependencies_logs_and_continues_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_auth_env(monkeypatch)
    calls: list[str] = []

    class _FailingVerifier:
        def warm(self) -> None:
            raise RuntimeError("jwks unavailable")

    monkeypatch.setattr(
        dependencies,
        "_get_jwt_verifier_cached",
        lambda **_kwargs: _FailingVerifier(),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_tracking_repository_cached",
        lambda **_kwargs: calls.append("tracking_repository"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_tracking_service_cached",
        lambda **_kwargs: calls.append("tracking_service"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_telegram_integration_service_cached",
        lambda **_kwargs: calls.append("telegram_service"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_user_service_cached",
        lambda **_kwargs: calls.append("user_service"),
    )
    monkeypatch.setattr(
        dependencies,
        "_get_billing_service_cached",
        lambda **_kwargs: calls.append("billing_service"),
    )

    with caplog.at_level(logging.WARNING, logger="scraper_api.auth"):
        dependencies.warm_application_dependencies(get_settings())

    assert "tracking_repository" in calls
    assert "warmup_failed dependency=jwt_verifier" in caplog.text
