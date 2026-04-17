from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field


class TrackedItemBootstrapRequest(BaseModel):
    url: AnyHttpUrl
    sites: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    fetch_mode: Literal["auto", "http", "dynamic", "stealth"] = "auto"
    check_interval_minutes: int = Field(default=60, ge=5, le=10080)
    target_amount: str | None = None


class TrackedItemUpdateRequest(BaseModel):
    status: Literal["active", "paused"] | None = None
    check_interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    target_amount: str | None = None


class PriceSnapshotResponse(BaseModel):
    snapshot_id: str
    item_id: str
    checked_at: str
    success: bool
    amount: str | None
    currency: str | None
    raw_text: str | None
    source: str | None
    error: str | None


class TrackedItemResponse(BaseModel):
    item_id: str
    source_url: str
    canonical_url: str | None
    product_id: str | None
    site_key: str | None
    adapter_key: str | None
    product_name: str | None
    brand: str | None
    model: str | None
    sku: str | None
    gtin: str | None
    image_url: str | None
    availability: str | None
    currency: str | None
    last_amount: str | None
    last_raw_text: str | None
    last_source: str | None
    status: Literal["active", "paused"]
    tracking_mode: Literal["monitor", "alert"]
    alerts_enabled: bool
    check_interval_minutes: int
    target_amount: str | None
    created_at: str
    updated_at: str
    last_checked_at: str | None
    next_check_at: str | None = None
    lease_until: str | None = None
    last_check_started_at: str | None = None
    last_check_finished_at: str | None = None


class TrackedItemListResponse(BaseModel):
    items: list[TrackedItemResponse]


class TrackedItemBootstrapResponse(BaseModel):
    request_id: str
    item: TrackedItemResponse


class TrackedItemDetailResponse(BaseModel):
    item: TrackedItemResponse
    recent_snapshots: list[PriceSnapshotResponse]


class TrackedItemUpdateResponse(BaseModel):
    item: TrackedItemResponse
