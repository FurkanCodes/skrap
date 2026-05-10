from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class TrackedItemBootstrapRequest(BaseModel):
    url: AnyHttpUrl | None = None
    scrape_result_ref: str | None = None
    selected_listing_id: str | None = None
    sites: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    fetch_mode: Literal["auto", "http", "dynamic", "stealth"] = "auto"
    check_interval_minutes: int = Field(default=60, ge=1, le=10080)
    target_amount: str | None = None
    notify_on_price_increase: bool | None = None
    notify_on_stock_change: bool | None = None
    duplicate_policy: Literal["upsert", "reject"] = "upsert"
    created_via: Literal["dashboard", "extension"] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "TrackedItemBootstrapRequest":
        if self.url is None and not (self.scrape_result_ref or "").strip():
            raise ValueError("Either 'url' or 'scrape_result_ref' is required")
        return self


class TrackedItemBulkBootstrapItemRequest(BaseModel):
    url: AnyHttpUrl | None = None
    scrape_result_ref: str | None = None
    selected_listing_id: str | None = None
    check_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    target_amount: str | None = None
    notify_on_price_increase: bool | None = None
    notify_on_stock_change: bool | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "TrackedItemBulkBootstrapItemRequest":
        if self.url is None and not (self.scrape_result_ref or "").strip():
            raise ValueError("Either 'url' or 'scrape_result_ref' is required")
        return self


class TrackedItemBulkBootstrapRequest(BaseModel):
    items: list[TrackedItemBulkBootstrapItemRequest] | None = Field(default=None, min_length=1)
    urls: list[AnyHttpUrl] | None = Field(default=None, min_length=1)
    sites: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    fetch_mode: Literal["auto", "http", "dynamic", "stealth"] = "auto"
    check_interval_minutes: int = Field(default=60, ge=1, le=10080)
    target_amount: str | None = None
    notify_on_price_increase: bool | None = None
    notify_on_stock_change: bool | None = None

    @model_validator(mode="after")
    def validate_item_sources(self) -> "TrackedItemBulkBootstrapRequest":
        if self.items is None and self.urls is None:
            raise ValueError("Either 'items' or 'urls' is required")
        if self.items is not None and self.urls is not None:
            raise ValueError("Use either 'items' or 'urls', not both")
        return self


class TrackedItemUpdateRequest(BaseModel):
    status: Literal["active", "paused"] | None = None
    check_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    target_amount: str | None = None
    notify_on_price_increase: bool | None = None
    notify_on_stock_change: bool | None = None


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
    availability: str | None = None


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
    tracked_listing_id: str | None = None
    tracked_listing_url: str | None = None
    created_via: Literal["dashboard", "extension"] | None = None
    outbound_url: str | None = None
    outbound_is_affiliate: bool = False
    status: Literal["active", "paused"]
    tracking_mode: Literal["monitor", "alert"]
    alerts_enabled: bool
    check_interval_minutes: int
    target_amount: str | None
    notify_on_price_increase: bool
    notify_on_stock_change: bool
    created_at: str
    updated_at: str
    last_checked_at: str | None
    next_check_at: str | None = None
    lease_until: str | None = None
    last_check_started_at: str | None = None
    last_check_finished_at: str | None = None


class TrackedItemListResponse(BaseModel):
    items: list[TrackedItemResponse]


class TrackedItemLookupResponse(BaseModel):
    item: TrackedItemResponse | None


class TrackedItemBootstrapResponse(BaseModel):
    request_id: str
    item: TrackedItemResponse


class TrackedItemBulkBootstrapResult(BaseModel):
    index: int = Field(ge=0)
    url: str | None
    success: bool
    item: TrackedItemResponse | None = None
    error: str | None = None


class TrackedItemBulkBootstrapResponse(BaseModel):
    request_id: str
    total_items: int
    success_count: int
    failure_count: int
    results: list[TrackedItemBulkBootstrapResult]


class TrackedItemDetailResponse(BaseModel):
    item: TrackedItemResponse
    recent_snapshots: list[PriceSnapshotResponse]


class TrackedItemUpdateResponse(BaseModel):
    item: TrackedItemResponse
