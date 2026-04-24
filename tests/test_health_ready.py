from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.api.dependencies import get_app_settings, get_jwt_verifier
from app.api.routers import health as health_router
from app.core.config import get_settings
from app.core.readiness import ComponentReadiness
from app.main import app


class _FakeVerifier:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    def warm(self) -> None:
        if self.should_fail:
            raise RuntimeError("jwks unavailable")


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_ready_returns_ok_when_dependencies_are_ready(monkeypatch) -> None:
    settings = replace(get_settings(), auth_required=True)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[get_jwt_verifier] = lambda: _FakeVerifier()
    monkeypatch.setattr(
        health_router,
        "check_postgres_ready",
        lambda _settings: ComponentReadiness(status="ok"),
    )
    monkeypatch.setattr(
        health_router,
        "check_redis_ready",
        lambda _settings: ComponentReadiness(status="ok"),
    )
    client = TestClient(app)

    response = client.get("/ready")
    _clear_overrides()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["checks"]["jwks"]["status"] == "ok"


def test_ready_returns_503_when_dependency_fails(monkeypatch) -> None:
    settings = replace(get_settings(), auth_required=True)
    app.dependency_overrides[get_app_settings] = lambda: settings
    app.dependency_overrides[get_jwt_verifier] = lambda: _FakeVerifier(should_fail=True)
    monkeypatch.setattr(
        health_router,
        "check_postgres_ready",
        lambda _settings: ComponentReadiness(status="ok"),
    )
    monkeypatch.setattr(
        health_router,
        "check_redis_ready",
        lambda _settings: ComponentReadiness(status="error", error="redis_unavailable"),
    )
    client = TestClient(app)

    response = client.get("/ready")
    _clear_overrides()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["redis"]["error"] == "redis_unavailable"
    assert body["checks"]["jwks"]["error"] == "jwks_unavailable"
