from __future__ import annotations

"""Tracking-specific price result normalization."""

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from app.scraper import PriceResult
from app.scraper.domain import PriceListing
from app.scraper.parsing import normalize_akakce_listing_url, normalize_url


def normalize_tracking_price_result(
    result: PriceResult,
    *,
    tracked_listing_id: str | None = None,
    tracked_listing_url: str | None = None,
    fallback_to_first: bool = True,
) -> PriceResult:
    """Collapse multi-listing scrape output to the one price tracking follows."""
    if not result.listings:
        return result

    primary = _select_tracked_listing(
        result,
        tracked_listing_id=tracked_listing_id,
        tracked_listing_url=tracked_listing_url,
    )
    if primary is None:
        if not fallback_to_first:
            return replace(
                result,
                success=False,
                amount=None,
                currency=None,
                raw_text=None,
                source=None,
                error="selected_listing_not_found",
            )
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


def _select_tracked_listing(
    result: PriceResult,
    *,
    tracked_listing_id: str | None,
    tracked_listing_url: str | None,
) -> PriceListing | None:
    listing_id = (tracked_listing_id or "").strip()
    if listing_id:
        for listing in result.listings:
            if listing.item_id == listing_id:
                return listing

    normalized_tracked_url = _normalize_listing_url(
        tracked_listing_url,
        base_url=result.url,
    )
    if normalized_tracked_url:
        for listing in result.listings:
            normalized_listing_url = _normalize_listing_url(
                listing.listing_url,
                base_url=result.url,
            )
            if normalized_listing_url == normalized_tracked_url:
                return listing
        return None

    if listing_id:
        return None

    return result.listings[0]


def _normalize_listing_url(value: str | None, *, base_url: str) -> str | None:
    listing_url = (value or "").strip()
    if not listing_url:
        return None
    akakce_listing_url = normalize_akakce_listing_url(base_url, listing_url)
    if akakce_listing_url is not None:
        return akakce_listing_url
    if listing_url.startswith("#"):
        return f"{normalize_url(base_url, keep_fragment=False)}{listing_url}"
    return normalize_url(urljoin(base_url, listing_url), keep_fragment=True)


def resolve_listing_url(value: str | None, *, base_url: str) -> str | None:
    """Resolve a listing URL for user-facing notifications."""
    listing_url = (value or "").strip()
    if not listing_url:
        return None
    if listing_url.startswith("#"):
        return f"{normalize_url(base_url, keep_fragment=False)}{listing_url}"
    return urljoin(base_url, listing_url)


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
