from __future__ import annotations

"""Tracking-specific price result normalization."""

from dataclasses import replace
from decimal import Decimal, InvalidOperation

from app.scraper import PriceResult


def normalize_tracking_price_result(result: PriceResult) -> PriceResult:
    """Collapse multi-listing scrape output to the one price tracking follows."""
    if not result.listings:
        return result

    primary = result.listings[0]
    if _parse_decimal(primary.amount) is None:
        return replace(
            result,
            amount=None,
            currency=primary.currency or result.currency,
            raw_text=None,
            source=None,
            listing_count=0,
            listings=[],
        )

    return replace(
        result,
        amount=primary.amount,
        currency=primary.currency or result.currency,
        raw_text=primary.raw_text,
        source=primary.source,
        listing_count=1,
        listings=[primary],
    )


def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None
