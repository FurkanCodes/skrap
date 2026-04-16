from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

FetchMode = Literal["auto", "http", "dynamic", "stealth"]


@dataclass(frozen=True)
class PriceCandidate:
    raw_text: str
    amount: Decimal
    currency: str | None
    source: str
    seller: str | None = None
    listing_url: str | None = None


@dataclass(frozen=True)
class PriceListing:
    item_id: str
    amount: str
    currency: str | None
    raw_text: str
    source: str
    seller: str | None = None
    listing_url: str | None = None


@dataclass(frozen=True)
class PriceResult:
    url: str
    success: bool
    product_id: str | None = None
    site_key: str | None = None
    adapter_key: str | None = None
    amount: str | None = None
    currency: str | None = None
    raw_text: str | None = None
    source: str | None = None
    listing_count: int = 0
    listings: list[PriceListing] = field(default_factory=list)
    error: str | None = None
