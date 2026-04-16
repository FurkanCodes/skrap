from __future__ import annotations

"""Shared domain models flowing across adapters, extractors, and API layers."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

FetchMode = Literal["auto", "http", "dynamic", "stealth"]


@dataclass(frozen=True)
class PriceCandidate:
    """Internal parsed candidate before stable listing IDs are assigned."""

    raw_text: str
    amount: Decimal
    currency: str | None
    source: str
    seller: str | None = None
    listing_url: str | None = None


@dataclass(frozen=True)
class PriceListing:
    """External listing representation with stable `item_id`."""

    item_id: str
    amount: str
    currency: str | None
    raw_text: str
    source: str
    seller: str | None = None
    listing_url: str | None = None


@dataclass(frozen=True)
class PriceResult:
    """Final per-URL scrape result returned to callers."""

    url: str
    success: bool
    product_id: str | None = None
    site_key: str | None = None
    adapter_key: str | None = None
    amount: str | None = None
    currency: str | None = None
    raw_text: str | None = None
    source: str | None = None
    product_name: str | None = None
    brand: str | None = None
    model: str | None = None
    sku: str | None = None
    gtin: str | None = None
    image_url: str | None = None
    canonical_url: str | None = None
    availability: str | None = None
    extracted_at: str | None = None
    listing_count: int = 0
    listings: list[PriceListing] = field(default_factory=list)
    error: str | None = None
