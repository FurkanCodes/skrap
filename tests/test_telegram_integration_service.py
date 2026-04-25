from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.application.telegram_integration_service import (
    TelegramIntegrationError,
    TelegramIntegrationService,
)
from app.tracking import TelegramConnectSession


@dataclass
class _FakeRepository:
    sessions: dict[str, TelegramConnectSession]
    active_chat_id: str | None = None

    def create_telegram_connect_session(self, session: TelegramConnectSession) -> TelegramConnectSession:
        self.sessions[session.state] = session
        return session

    def get_telegram_connect_session(self, state: str) -> TelegramConnectSession | None:
        return self.sessions.get(state)

    def mark_telegram_connect_session_connected(
        self,
        *,
        state: str,
        connected_at: str,
        telegram_user_id: str,
        telegram_chat_id: str,
        telegram_username: str | None,
    ) -> TelegramConnectSession | None:
        session = self.sessions.get(state)
        if session is None:
            return None
        if session.status != "pending":
            return None
        updated = TelegramConnectSession(
            user_id=session.user_id,
            state=session.state,
            status="connected",
            created_at=session.created_at,
            expires_at=session.expires_at,
            connected_at=connected_at,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_username=telegram_username,
        )
        self.sessions[state] = updated
        return updated

    def mark_telegram_connect_session_expired(self, *, state: str) -> TelegramConnectSession | None:
        session = self.sessions.get(state)
        if session is None:
            return None
        updated = TelegramConnectSession(
            user_id=session.user_id,
            state=session.state,
            status="expired",
            created_at=session.created_at,
            expires_at=session.expires_at,
            connected_at=session.connected_at,
            telegram_user_id=session.telegram_user_id,
            telegram_chat_id=session.telegram_chat_id,
            telegram_username=session.telegram_username,
        )
        self.sessions[state] = updated
        return updated

    def set_active_telegram_chat(self, *, chat_id: str, updated_at: str) -> None:  # noqa: ARG002
        self.active_chat_id = chat_id

    def get_active_telegram_chat(self) -> str | None:
        return self.active_chat_id


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def test_start_connect_requires_bot_token() -> None:
    service = TelegramIntegrationService(
        repository=_FakeRepository(sessions={}),
        bot_token="",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )

    try:
        service.start_connect()
        assert False, "Expected TelegramIntegrationError"
    except TelegramIntegrationError as exc:
        assert str(exc) == "TELEGRAM_BOT_TOKEN is required"


def test_start_connect_uses_username_resolved_from_token(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepository(sessions={})
    service = TelegramIntegrationService(
        repository=repo,
        bot_token="token",
        bot_username="Dealvinci",
        connect_ttl_minutes=15,
    )
    calls = 0

    def _fake_get(url, timeout):  # noqa: ANN001
        nonlocal calls
        calls += 1
        assert url == "https://api.telegram.org/bottoken/getMe"
        assert timeout == 10
        return httpx.Response(
            200,
            json={"ok": True, "result": {"username": "SkkrappyBot", "is_bot": True}},
        )

    monkeypatch.setattr("app.application.telegram_integration_service.httpx.get", _fake_get)

    output = service.start_connect()
    second_output = service.start_connect()

    assert output.bot_username == "SkkrappyBot"
    assert output.deep_link_url.startswith("https://t.me/SkkrappyBot?start=")
    assert output.state in repo.sessions
    assert repo.sessions[output.state].status == "pending"
    assert second_output.bot_username == "SkkrappyBot"
    assert calls == 1


def test_start_connect_logs_configured_username_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = TelegramIntegrationService(
        repository=_FakeRepository(sessions={}),
        bot_token="token",
        bot_username="Dealvinci",
        connect_ttl_minutes=15,
    )

    def _fake_get(url, timeout):  # noqa: ANN001, ARG001
        return httpx.Response(200, json={"ok": True, "result": {"username": "SkkrappyBot"}})

    monkeypatch.setattr("app.application.telegram_integration_service.httpx.get", _fake_get)

    with caplog.at_level("WARNING", logger="scraper_api.telegram"):
        output = service.start_connect()

    assert output.bot_username == "SkkrappyBot"
    assert "telegram_bot_username_mismatch" in caplog.text
    assert "configured=Dealvinci actual=SkkrappyBot" in caplog.text


def test_start_connect_rejects_invalid_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    service = TelegramIntegrationService(
        repository=_FakeRepository(sessions={}),
        bot_token="bad-token",
        bot_username="Dealvinci",
        connect_ttl_minutes=15,
    )

    def _fake_get(url, timeout):  # noqa: ANN001, ARG001
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})

    monkeypatch.setattr("app.application.telegram_integration_service.httpx.get", _fake_get)

    try:
        service.start_connect()
        assert False, "Expected TelegramIntegrationError"
    except TelegramIntegrationError as exc:
        assert str(exc) == "TELEGRAM_BOT_TOKEN is invalid"


def test_process_webhook_update_connects_session_sets_active_chat_and_sends_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = "state_123"
    sent_payloads: list[dict] = []
    repo = _FakeRepository(
        sessions={
            state: TelegramConnectSession(
                state=state,
                status="pending",
                created_at=_now_iso(),
                expires_at=(datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                connected_at=None,
                telegram_user_id=None,
                telegram_chat_id=None,
                telegram_username=None,
            )
        }
    )
    service = TelegramIntegrationService(
        repository=repo,
        bot_token="secret-token-123",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )

    def _fake_post(url, json, timeout):  # noqa: ANN001
        assert url == "https://api.telegram.org/botsecret-token-123/sendMessage"
        assert timeout == 10
        sent_payloads.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr("app.application.telegram_integration_service.httpx.post", _fake_post)

    processed = service.process_webhook_update(
        {
            "message": {
                "text": "/start state_123",
                "from": {"id": 999, "username": "tester"},
                "chat": {"id": 12345},
            }
        }
    )

    assert processed is True
    assert repo.sessions[state].status == "connected"
    assert repo.sessions[state].telegram_chat_id == "12345"
    assert repo.active_chat_id == "12345"
    assert sent_payloads == [
        {
            "chat_id": "12345",
            "text": "Telegram connected securely. You will receive Dealvinci price alerts in this chat.",
            "disable_web_page_preview": True,
        }
    ]


def test_process_webhook_update_does_not_resend_confirmation_for_duplicate_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = "state_123"
    sent_payloads: list[dict] = []
    repo = _FakeRepository(
        sessions={
            state: TelegramConnectSession(
                state=state,
                status="pending",
                created_at=_now_iso(),
                expires_at=(datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                connected_at=None,
                telegram_user_id=None,
                telegram_chat_id=None,
                telegram_username=None,
            )
        }
    )
    service = TelegramIntegrationService(
        repository=repo,
        bot_token="secret-token-123",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )

    def _fake_post(url, json, timeout):  # noqa: ANN001, ARG001
        sent_payloads.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    update = {
        "message": {
            "text": "/start state_123",
            "from": {"id": 999, "username": "tester"},
            "chat": {"id": 12345},
        }
    }
    monkeypatch.setattr("app.application.telegram_integration_service.httpx.post", _fake_post)

    first_processed = service.process_webhook_update(update)
    second_processed = service.process_webhook_update(update)

    assert first_processed is True
    assert second_processed is False
    assert len(sent_payloads) == 1


def test_process_webhook_update_keeps_connection_when_confirmation_send_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = "state_123"
    repo = _FakeRepository(
        sessions={
            state: TelegramConnectSession(
                state=state,
                status="pending",
                created_at=_now_iso(),
                expires_at=(datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
                connected_at=None,
                telegram_user_id=None,
                telegram_chat_id=None,
                telegram_username=None,
            )
        }
    )
    service = TelegramIntegrationService(
        repository=repo,
        bot_token="secret-token-123",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )

    def _fake_post(url, json, timeout):  # noqa: ANN001, ARG001
        raise RuntimeError("request failed for secret-token-123")

    monkeypatch.setattr("app.application.telegram_integration_service.httpx.post", _fake_post)

    with caplog.at_level("WARNING", logger="scraper_api.telegram"):
        processed = service.process_webhook_update(
            {
                "message": {
                    "text": "/start state_123",
                    "from": {"id": 999, "username": "tester"},
                    "chat": {"id": 12345},
                }
            }
        )

    assert processed is True
    assert repo.sessions[state].status == "connected"
    assert repo.active_chat_id == "12345"
    assert "telegram_connect_confirmation_failed" in caplog.text
    assert "secret-token-123" not in caplog.text
    assert "<redacted>" in caplog.text


def test_process_webhook_update_returns_false_for_expired_state() -> None:
    state = "state_expired"
    repo = _FakeRepository(
        sessions={
            state: TelegramConnectSession(
                state=state,
                status="pending",
                created_at=_now_iso(),
                expires_at=(datetime.now(tz=UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                connected_at=None,
                telegram_user_id=None,
                telegram_chat_id=None,
                telegram_username=None,
            )
        }
    )
    service = TelegramIntegrationService(
        repository=repo,
        bot_token="token",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )

    processed = service.process_webhook_update(
        {
            "message": {
                "text": "/start state_expired",
                "from": {"id": 1000, "username": "tester"},
                "chat": {"id": 99999},
            }
        }
    )

    assert processed is False
    assert repo.sessions[state].status == "expired"
    assert repo.active_chat_id is None


def test_setup_webhook_requires_https_url() -> None:
    service = TelegramIntegrationService(
        repository=_FakeRepository(sessions={}),
        bot_token="token",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )
    try:
        service.setup_webhook(
            public_base_url="http://example.com",
            api_prefix="/api/v1",
            webhook_secret="secret",
        )
        assert False, "Expected TelegramIntegrationError"
    except TelegramIntegrationError as exc:
        assert str(exc) == "public_base_url must use https:// for Telegram webhook"


def test_setup_webhook_success(monkeypatch) -> None:
    service = TelegramIntegrationService(
        repository=_FakeRepository(sessions={}),
        bot_token="token",
        bot_username="mypricebot",
        connect_ttl_minutes=15,
    )

    def _fake_post(url, json, timeout):  # noqa: ANN001
        assert url == "https://api.telegram.org/bottoken/setWebhook"
        assert json["url"] == "https://example.com/api/v1/integrations/telegram/webhook"
        assert json["secret_token"] == "secret"
        assert timeout == 10
        return httpx.Response(200, json={"ok": True, "description": "Webhook was set"})

    monkeypatch.setattr("app.application.telegram_integration_service.httpx.post", _fake_post)

    output = service.setup_webhook(
        public_base_url="https://example.com",
        api_prefix="/api/v1",
        webhook_secret="secret",
    )

    assert output.ok is True
    assert output.webhook_url == "https://example.com/api/v1/integrations/telegram/webhook"
