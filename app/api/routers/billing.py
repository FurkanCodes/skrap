from __future__ import annotations

from dataclasses import asdict
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.api.dependencies import (
    get_app_settings,
    get_billing_service,
    get_current_principal,
    get_user_service,
)
from app.api.schemas.billing import (
    BillingCurrentResponse,
    BillingSubscriptionResponse,
    PaddleBillingEventRequest,
    PaddleBillingEventResponse,
)
from app.application.billing_service import (
    BillingService,
    BillingServiceError,
    PaddleBillingEventInput,
)
from app.application.user_service import UserService
from app.core.auth import AuthPrincipal
from app.core.config import Settings
from app.tracking import BillingSubscription

router = APIRouter(prefix="/billing", tags=["billing"])
_logger = logging.getLogger("scraper_api.billing")
_REJECTED_REASONS = {"rejected_plan_mismatch", "missing_price_config"}


@router.get("/current", response_model=BillingCurrentResponse)
async def get_current_billing(
    principal: AuthPrincipal = Depends(get_current_principal),
    user_service: UserService = Depends(get_user_service),
    billing_service: BillingService = Depends(get_billing_service),
) -> BillingCurrentResponse:
    current_user = await run_in_threadpool(user_service.provision_from_principal, principal)
    subscription = await run_in_threadpool(
        billing_service.get_current_subscription,
        user_id=current_user.user_id,
    )
    return BillingCurrentResponse(subscription=_subscription_response(subscription))


@router.post("/paddle/events", response_model=PaddleBillingEventResponse)
async def process_paddle_event(
    payload: PaddleBillingEventRequest,
    settings: Settings = Depends(get_app_settings),
    billing_service: BillingService = Depends(get_billing_service),
    x_billing_backend_secret: str | None = Header(default=None),
) -> PaddleBillingEventResponse:
    expected_secret = settings.billing_backend_secret.strip()
    if expected_secret and x_billing_backend_secret != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid billing backend secret")

    event = PaddleBillingEventInput(**payload.model_dump())
    try:
        output = await run_in_threadpool(billing_service.process_paddle_event, event)
    except BillingServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _logger.info(
        "paddle_event_response event_id=%s event_type=%s processed=%s reason=%s",
        payload.event_id,
        payload.event_type,
        output.processed,
        output.reason,
    )
    return PaddleBillingEventResponse(
        ok=output.reason not in _REJECTED_REASONS,
        processed=output.processed,
        reason=output.reason,
        subscription=_subscription_response(output.subscription),
    )


def _subscription_response(
    subscription: BillingSubscription | None,
) -> BillingSubscriptionResponse | None:
    if subscription is None:
        return None
    return BillingSubscriptionResponse.model_validate(asdict(subscription))
