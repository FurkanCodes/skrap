from __future__ import annotations

from dataclasses import dataclass, replace

from fastapi.testclient import TestClient

from app.api.dependencies import get_app_settings, get_telegram_integration_service
from app.application.telegram_integration_service import (
    TelegramConnectStartOutput,
    TelegramConnectStatusOutput,
    TelegramIntegrationError,
    TelegramWebhookSetupOutput,
)
from app.core.config import get_settings
from app.main import app


@dataclass
class _FakeTelegramService:
    started: bool = False
    processed_updates: int = 0

    def start_connect(self, *, user_id: str | None = None) -> TelegramConnectStartOutput:  # noqa: ARG002
        self.started = True
        return TelegramConnectStartOutput(
            state="state_123",
            deep_link_url="https://t.me/testbot?start=state_123",
            expires_at="2026-04-18T00:00:00Z",
            bot_username="testbot",
        )

    def get_connect_status(
        self,
        *,
        user_id: str | None = None,  # noqa: ARG002
        state: str,
    ) -> TelegramConnectStatusOutput | None:
        if state == "missing":
            return None
        return TelegramConnectStatusOutput(
            state=state,
            status="connected",
            expires_at="2026-04-18T00:00:00Z",
            connected_at="2026-04-17T23:00:00Z",
            telegram_chat_id="12345",
            telegram_username="tester",
        )

    def get_active_chat_status(self, *, user_id: str | None = None) -> tuple[bool, str | None]:  # noqa: ARG002
        return (True, "12345")

    def process_webhook_update(self, payload: dict) -> bool:
        self.processed_updates += 1
        return bool(payload)

    def setup_webhook(
        self,
        *,
        public_base_url: str,
        api_prefix: str,
        webhook_secret: str | None,
    ) -> TelegramWebhookSetupOutput:
        return TelegramWebhookSetupOutput(
            webhook_url=f"{public_base_url.rstrip('/')}{api_prefix}/integrations/telegram/webhook",
            ok=True,
            description="Webhook was set",
        )


@dataclass
class _FailingTelegramService:
    def start_connect(self, *, user_id: str | None = None) -> TelegramConnectStartOutput:  # noqa: ARG002
        raise TelegramIntegrationError("TELEGRAM_BOT_USERNAME is required")


def _build_client(service: object, *, webhook_secret: str | None = "") -> TestClient:
    app.dependency_overrides[get_telegram_integration_service] = lambda: service
    if webhook_secret is not None:
        settings = replace(get_settings(), telegram_webhook_secret=webhook_secret)
        app.dependency_overrides[get_app_settings] = lambda: settings
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_start_connect_returns_deep_link() -> None:
    client = _build_client(_FakeTelegramService())
    response = client.post("/api/v1/integrations/telegram/connect")
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "state_123"
    assert body["deep_link_url"] == "https://t.me/testbot?start=state_123"
    assert body["bot_username"] == "testbot"


def test_start_connect_maps_service_error_to_422() -> None:
    client = _build_client(_FailingTelegramService())
    response = client.post("/api/v1/integrations/telegram/connect")
    _clear_overrides()

    assert response.status_code == 422
    assert response.json()["detail"] == "TELEGRAM_BOT_USERNAME is required"


def test_setup_webhook_returns_configured_url() -> None:
    client = _build_client(_FakeTelegramService())
    response = client.post(
        "/api/v1/integrations/telegram/setup-webhook",
        json={"public_base_url": "https://example.com"},
    )
    _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["webhook_url"] == "https://example.com/api/v1/integrations/telegram/webhook"


def test_get_connect_status_and_not_found() -> None:
    client = _build_client(_FakeTelegramService())

    ok_response = client.get("/api/v1/integrations/telegram/connect/state_123")
    missing_response = client.get("/api/v1/integrations/telegram/connect/missing")
    _clear_overrides()

    assert ok_response.status_code == 200
    assert ok_response.json()["status"] == "connected"

    assert missing_response.status_code == 404
    assert missing_response.json()["detail"] == "connect session not found"


def test_get_telegram_status() -> None:
    client = _build_client(_FakeTelegramService())
    response = client.get("/api/v1/integrations/telegram/status")
    _clear_overrides()

    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert response.json()["active_chat_id"] == "12345"


def test_webhook_requires_secret_when_configured() -> None:
    client = _build_client(_FakeTelegramService(), webhook_secret="")
    response = client.post(
        "/api/v1/integrations/telegram/webhook",
        json={"message": {"text": "/start state_123"}},
    )
    _clear_overrides()

    # Secret is optional by default in config. This request should pass.
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_webhook_processed_flag() -> None:
    service = _FakeTelegramService()
    client = _build_client(service, webhook_secret="")
    response = client.post(
        "/api/v1/integrations/telegram/webhook",
        json={"message": {"text": "/start state_123"}},
    )
    _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": True}
    assert service.processed_updates == 1


def test_webhook_rejects_invalid_secret_token() -> None:
    service = _FakeTelegramService()
    settings = replace(
        get_settings(),
        telegram_webhook_secret="secret-token",
    )
    app.dependency_overrides[get_telegram_integration_service] = lambda: service
    app.dependency_overrides[get_app_settings] = lambda: settings
    client = TestClient(app)

    response = client.post(
        "/api/v1/integrations/telegram/webhook",
        json={"message": {"text": "/start state_123"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    _clear_overrides()

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Telegram webhook secret"
