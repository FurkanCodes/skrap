from __future__ import annotations

from pydantic import BaseModel


class PaddleBillingEventRequest(BaseModel):
    event_id: str
    event_type: str
    occurred_at: str
    user_id: str | None = None
    provider_customer_id: str | None = None
    provider_subscription_id: str | None = None
    provider_price_id: str | None = None
    plan_key: str | None = None
    status: str | None = None
    current_period_start: str | None = None
    current_period_end: str | None = None
    next_billed_at: str | None = None
    cancel_at_period_end: bool = False


class BillingSubscriptionResponse(BaseModel):
    user_id: str
    provider: str
    provider_customer_id: str | None
    provider_subscription_id: str | None
    provider_price_id: str | None
    plan_key: str | None
    status: str
    current_period_start: str | None
    current_period_end: str | None
    next_billed_at: str | None
    cancel_at_period_end: bool
    updated_at: str


class PaddleBillingEventResponse(BaseModel):
    ok: bool
    processed: bool
    reason: str | None = None
    subscription: BillingSubscriptionResponse | None = None


class BillingCurrentResponse(BaseModel):
    subscription: BillingSubscriptionResponse | None
