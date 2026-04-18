from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import httpx
import json

from app.tracking import TelegramConnectSession, TrackingRepository


class TelegramIntegrationError(RuntimeError):
    """Base error for Telegram connection flows."""


@dataclass(frozen=True)
class TelegramConnectStartOutput:
    state: str
    deep_link_url: str
    expires_at: str
    bot_username: str


@dataclass(frozen=True)
class TelegramConnectStatusOutput:
    state: str
    status: str
    expires_at: str
    connected_at: str | None
    telegram_chat_id: str | None
    telegram_username: str | None


@dataclass(frozen=True)
class TelegramWebhookSetupOutput:
    webhook_url: str
    ok: bool
    description: str | None


class TelegramIntegrationService:
    """Owns Telegram connect session lifecycle and webhook handshake."""

    def __init__(
        self,
        *,
        repository: TrackingRepository,
        bot_token: str,
        bot_username: str,
        connect_ttl_minutes: int,
    ) -> None:
        self._repository = repository
        self._bot_token = bot_token.strip()
        self._bot_username = bot_username.strip().lstrip("@")
        self._connect_ttl_minutes = max(1, connect_ttl_minutes)

    def start_connect(self, *, user_id: str = "legacy_owner") -> TelegramConnectStartOutput:
        if not self._bot_username:
            raise TelegramIntegrationError("TELEGRAM_BOT_USERNAME is required")

        now = _utcnow()
        expires_at = now + timedelta(minutes=self._connect_ttl_minutes)
        state = secrets.token_urlsafe(24)
        session = TelegramConnectSession(
            user_id=user_id,
            state=state,
            status="pending",
            created_at=_to_iso(now),
            expires_at=_to_iso(expires_at),
            connected_at=None,
            telegram_user_id=None,
            telegram_chat_id=None,
            telegram_username=None,
        )
        saved = self._repository.create_telegram_connect_session(session)
        return TelegramConnectStartOutput(
            state=saved.state,
            deep_link_url=f"https://t.me/{self._bot_username}?start={saved.state}",
            expires_at=saved.expires_at,
            bot_username=self._bot_username,
        )

    def get_connect_status(
        self,
        *,
        user_id: str = "legacy_owner",
        state: str,
    ) -> TelegramConnectStatusOutput | None:
        session = self._get_connect_session(user_id=user_id, state=state)
        if session is None:
            return None

        session = self._expire_if_needed(session)
        return TelegramConnectStatusOutput(
            state=session.state,
            status=session.status,
            expires_at=session.expires_at,
            connected_at=session.connected_at,
            telegram_chat_id=session.telegram_chat_id,
            telegram_username=session.telegram_username,
        )

    def get_active_chat_status(self, *, user_id: str = "legacy_owner") -> tuple[bool, str | None]:
        active_chat = self._get_active_chat(user_id=user_id)
        return (active_chat is not None, active_chat)

    def setup_webhook(
        self,
        *,
        public_base_url: str,
        api_prefix: str,
        webhook_secret: str | None,
    ) -> TelegramWebhookSetupOutput:
        if not self._bot_token:
            raise TelegramIntegrationError("TELEGRAM_BOT_TOKEN is required")

        base = public_base_url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise TelegramIntegrationError("public_base_url must start with http:// or https://")
        if not base.startswith("https://"):
            raise TelegramIntegrationError("public_base_url must use https:// for Telegram webhook")

        prefix = api_prefix.strip()
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        webhook_url = f"{base}{prefix}/integrations/telegram/webhook"

        api_url = f"https://api.telegram.org/bot{self._bot_token}/setWebhook"
        payload: dict[str, object] = {"url": webhook_url}
        token = (webhook_secret or "").strip()
        if token:
            payload["secret_token"] = token

        try:
            response = httpx.post(api_url, json=payload, timeout=10)
        except Exception as exc:
            raise TelegramIntegrationError(f"telegram_set_webhook_network_error: {exc}") from exc

        body = _json_dict(response.text)
        ok = bool(body.get("ok", False))
        description = str(body.get("description", "")).strip() or None
        if not response.is_success or not ok:
            detail = description or "telegram_set_webhook_failed"
            raise TelegramIntegrationError(f"telegram_set_webhook_failed: {detail}")

        return TelegramWebhookSetupOutput(
            webhook_url=webhook_url,
            ok=True,
            description=description,
        )

    def process_webhook_update(self, payload: dict) -> bool:
        start_payload = _extract_start_payload(payload)
        if start_payload is None:
            return False

        state = start_payload.state
        session = self._get_connect_session_by_state(state=state)
        if session is None:
            return False

        session = self._expire_if_needed(session)
        if session.status == "expired":
            return False

        connected_at = _to_iso(_utcnow())
        updated = self._mark_connect_session_connected(
            user_id=session.user_id,
            state=state,
            connected_at=connected_at,
            telegram_user_id=start_payload.user_id,
            telegram_chat_id=start_payload.chat_id,
            telegram_username=start_payload.username,
        )
        if updated is None:
            return False

        self._set_active_chat(
            user_id=session.user_id,
            chat_id=start_payload.chat_id,
            updated_at=connected_at,
        )
        return True

    def _expire_if_needed(self, session: TelegramConnectSession) -> TelegramConnectSession:
        if session.status != "pending":
            return session

        now = _utcnow()
        expires_at = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
        if now <= expires_at:
            return session

        expired = self._mark_connect_session_expired(
            user_id=session.user_id,
            state=session.state,
        )
        if expired is not None:
            return expired
        refreshed = self._get_connect_session(
            user_id=session.user_id,
            state=session.state,
        )
        return refreshed or session

    def _get_connect_session(
        self,
        *,
        user_id: str,
        state: str,
    ) -> TelegramConnectSession | None:
        method = getattr(self._repository, "get_telegram_connect_session")
        try:
            return method(user_id=user_id, state=state)
        except TypeError:
            return method(state)

    def _get_connect_session_by_state(self, *, state: str) -> TelegramConnectSession | None:
        method = getattr(self._repository, "get_telegram_connect_session_by_state", None)
        if callable(method):
            return method(state)
        # Compatibility path for older repositories/tests.
        return self._get_connect_session(user_id="legacy_owner", state=state)

    def _mark_connect_session_connected(
        self,
        *,
        user_id: str,
        state: str,
        connected_at: str,
        telegram_user_id: str,
        telegram_chat_id: str,
        telegram_username: str | None,
    ) -> TelegramConnectSession | None:
        method = getattr(self._repository, "mark_telegram_connect_session_connected")
        try:
            return method(
                user_id=user_id,
                state=state,
                connected_at=connected_at,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_username=telegram_username,
            )
        except TypeError:
            return method(
                state=state,
                connected_at=connected_at,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_username=telegram_username,
            )

    def _mark_connect_session_expired(
        self,
        *,
        user_id: str,
        state: str,
    ) -> TelegramConnectSession | None:
        method = getattr(self._repository, "mark_telegram_connect_session_expired")
        try:
            return method(user_id=user_id, state=state)
        except TypeError:
            return method(state=state)

    def _set_active_chat(self, *, user_id: str, chat_id: str, updated_at: str) -> None:
        method = getattr(self._repository, "set_active_telegram_chat")
        try:
            method(user_id=user_id, chat_id=chat_id, updated_at=updated_at)
        except TypeError:
            method(chat_id=chat_id, updated_at=updated_at)

    def _get_active_chat(self, *, user_id: str) -> str | None:
        method = getattr(self._repository, "get_active_telegram_chat")
        try:
            return method(user_id=user_id)
        except TypeError:
            return method()


@dataclass(frozen=True)
class _StartPayload:
    state: str
    user_id: str
    chat_id: str
    username: str | None


def _extract_start_payload(payload: dict) -> _StartPayload | None:
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        return None

    text = str(message.get("text", "")).strip()
    if not text.startswith("/start"):
        return None

    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None

    state = parts[1].strip()
    if not state:
        return None

    from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    user_id = from_user.get("id")
    if chat_id is None or user_id is None:
        return None

    username = from_user.get("username")
    username_str = str(username).strip() if username is not None else None
    return _StartPayload(
        state=state,
        user_id=str(user_id),
        chat_id=str(chat_id),
        username=username_str or None,
    )


def _json_dict(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
