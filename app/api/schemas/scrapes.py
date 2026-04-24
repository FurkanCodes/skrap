from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field


class HealthResponse(BaseModel):
    status: str


class ComponentHealthResponse(BaseModel):
    status: Literal["ok", "error", "skipped"]
    error: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "error"]
    checks: dict[str, ComponentHealthResponse]


class SiteInfoResponse(BaseModel):
    key: str
    description: str


class SitesResponse(BaseModel):
    sites: list[SiteInfoResponse]


class PriceListingResponse(BaseModel):
    item_id: str
    amount: str
    currency: str | None
    raw_text: str
    source: str
    seller: str | None
    listing_url: str | None


class PriceResultResponse(BaseModel):
    result_ref: str | None = None
    url: str
    success: bool
    product_id: str | None
    site_key: str | None
    adapter_key: str | None
    amount: str | None
    currency: str | None
    raw_text: str | None
    source: str | None
    product_name: str | None = None
    brand: str | None = None
    model: str | None = None
    sku: str | None = None
    gtin: str | None = None
    image_url: str | None = None
    canonical_url: str | None = None
    availability: str | None = None
    extracted_at: str | None = None
    listing_count: int
    listings: list[PriceListingResponse]
    error: str | None


class ScrapeBatchRequest(BaseModel):
    urls: list[AnyHttpUrl] = Field(min_length=1)
    sites: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    fetch_mode: Literal["auto", "http", "dynamic", "stealth"] = "auto"


class ScrapeBatchResponse(BaseModel):
    request_id: str
    scrape_ref: str | None = None
    total_urls: int
    success_count: int
    failure_count: int
    results: list[PriceResultResponse]


class DiscoveryStatsResponse(BaseModel):
    requests_count: int
    failed_requests_count: int
    blocked_requests_count: int
    offsite_requests_count: int
    elapsed_seconds: float
    sessions_requests_count: dict[str, int]


class DiscoverBatchRequest(BaseModel):
    start_urls: list[AnyHttpUrl] = Field(min_length=1)
    sites: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    fetch_mode: Literal["auto", "http", "dynamic", "stealth"] = "auto"
    max_pages: int = Field(default=50, ge=1, le=300)
    max_depth: int = Field(default=2, ge=0, le=5)


class DiscoverBatchResponse(BaseModel):
    request_id: str
    seed_count: int
    discovered_url_count: int
    discovered_urls: list[str]
    discovery_stats: DiscoveryStatsResponse
    total_urls: int
    success_count: int
    failure_count: int
    results: list[PriceResultResponse]
