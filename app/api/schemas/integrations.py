from __future__ import annotations

from pydantic import BaseModel, HttpUrl


class TelegramConnectStartResponse(BaseModel):
    request_id: str
    state: str
    deep_link_url: str
    expires_at: str
    bot_username: str


class TelegramConnectStatusResponse(BaseModel):
    state: str
    status: str
    expires_at: str
    connected_at: str | None
    telegram_chat_id: str | None
    telegram_username: str | None


class TelegramActiveStatusResponse(BaseModel):
    connected: bool
    active_chat_id: str | None
    bot_username: str


class TelegramWebhookResponse(BaseModel):
    ok: bool
    processed: bool


class TelegramWebhookSetupRequest(BaseModel):
    public_base_url: HttpUrl


class TelegramWebhookSetupResponse(BaseModel):
    request_id: str
    ok: bool
    webhook_url: str
    description: str | None
