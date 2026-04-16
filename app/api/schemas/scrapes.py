from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, Field


class HealthResponse(BaseModel):
    status: str


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
    url: str
    success: bool
    product_id: str | None
    site_key: str | None
    adapter_key: str | None
    amount: str | None
    currency: str | None
    raw_text: str | None
    source: str | None
    listing_count: int
    listings: list[PriceListingResponse]
    error: str | None


class ScrapeBatchRequest(BaseModel):
    urls: list[AnyHttpUrl] = Field(min_length=1)
    sites: list[str] | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)


class ScrapeBatchResponse(BaseModel):
    request_id: str
    total_urls: int
    success_count: int
    failure_count: int
    results: list[PriceResultResponse]

