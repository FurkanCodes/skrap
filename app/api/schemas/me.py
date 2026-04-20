from __future__ import annotations

from pydantic import BaseModel


class MeUserResponse(BaseModel):
    user_id: str
    auth_provider: str
    auth_subject: str
    email: str | None
    display_name: str | None
    created_at: str
    updated_at: str


class MeEntitlementResponse(BaseModel):
    user_id: str
    plan_key: str
    max_active_trackers: int
    min_check_interval_minutes: int
    monthly_check_units: int
    allowed_channels: str
    overage_policy: str
    created_at: str
    updated_at: str


class MeUsageResponse(BaseModel):
    user_id: str
    period_start: str
    period_end: str
    monthly_check_units: int
    consumed_check_units: int
    remaining_check_units: int


class MeResponse(BaseModel):
    user: MeUserResponse
    entitlement: MeEntitlementResponse
    usage: MeUsageResponse
