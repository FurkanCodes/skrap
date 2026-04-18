from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_app_settings,
    get_telegram_integration_service,
)
from app.api.schemas.integrations import (
    TelegramActiveStatusResponse,
    TelegramConnectStartResponse,
    TelegramConnectStatusResponse,
    TelegramWebhookSetupRequest,
    TelegramWebhookSetupResponse,
    TelegramWebhookResponse,
)
from app.application.telegram_integration_service import (
    TelegramIntegrationError,
    TelegramIntegrationService,
)
from app.core.config import Settings

router = APIRouter(prefix="/integrations/telegram", tags=["integrations:telegram"])


@router.post("/setup-webhook", response_model=TelegramWebhookSetupResponse)
async def setup_telegram_webhook(
    payload: TelegramWebhookSetupRequest,
    request: Request,
    settings: Settings = Depends(get_app_settings),
    service: TelegramIntegrationService = Depends(get_telegram_integration_service),
) -> TelegramWebhookSetupResponse:
    request_id = str(getattr(request.state, "request_id", ""))
    try:
        output = await run_in_threadpool(
            service.setup_webhook,
            public_base_url=str(payload.public_base_url),
            api_prefix=settings.api_prefix,
            webhook_secret=settings.telegram_webhook_secret,
        )
    except TelegramIntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return TelegramWebhookSetupResponse(
        request_id=request_id,
        ok=output.ok,
        webhook_url=output.webhook_url,
        description=output.description,
    )


@router.post("/connect", response_model=TelegramConnectStartResponse)
async def start_telegram_connect(
    request: Request,
    service: TelegramIntegrationService = Depends(get_telegram_integration_service),
) -> TelegramConnectStartResponse:
    request_id = str(getattr(request.state, "request_id", ""))
    try:
        output = await run_in_threadpool(service.start_connect)
    except TelegramIntegrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return TelegramConnectStartResponse(
        request_id=request_id,
        state=output.state,
        deep_link_url=output.deep_link_url,
        expires_at=output.expires_at,
        bot_username=output.bot_username,
    )


@router.get("/connect/{state}", response_model=TelegramConnectStatusResponse)
async def get_telegram_connect_status(
    state: str,
    service: TelegramIntegrationService = Depends(get_telegram_integration_service),
) -> TelegramConnectStatusResponse:
    output = await run_in_threadpool(service.get_connect_status, state)
    if output is None:
        raise HTTPException(status_code=404, detail="connect session not found")

    return TelegramConnectStatusResponse(
        state=output.state,
        status=output.status,
        expires_at=output.expires_at,
        connected_at=output.connected_at,
        telegram_chat_id=output.telegram_chat_id,
        telegram_username=output.telegram_username,
    )


@router.get("/status", response_model=TelegramActiveStatusResponse)
async def get_telegram_status(
    settings: Settings = Depends(get_app_settings),
    service: TelegramIntegrationService = Depends(get_telegram_integration_service),
) -> TelegramActiveStatusResponse:
    connected, active_chat_id = await run_in_threadpool(service.get_active_chat_status)
    return TelegramActiveStatusResponse(
        connected=connected,
        active_chat_id=active_chat_id,
        bot_username=settings.telegram_bot_username,
    )


@router.post("/webhook", response_model=TelegramWebhookResponse)
async def telegram_webhook(
    payload: dict,
    settings: Settings = Depends(get_app_settings),
    service: TelegramIntegrationService = Depends(get_telegram_integration_service),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> TelegramWebhookResponse:
    expected_secret = settings.telegram_webhook_secret.strip()
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")

    processed = await run_in_threadpool(service.process_webhook_update, payload)
    return TelegramWebhookResponse(ok=True, processed=processed)
