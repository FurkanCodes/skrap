from __future__ import annotations

"""Main scraper orchestration service.

This module is the application-facing entry point for scraping. It coordinates:
- URL/site resolution
- fetcher mode selection/escalation
- adapter execution
- final result shaping
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from .constants import SUPPORTED_FETCH_MODES
from .domain import PriceListing, PriceResult
from .fetching import auto_followup_modes, detect_auto_escalation_signals, fetch_page_with_mode
from .inputs import parse_fetch_mode
from .metadata import extract_product_metadata
from .parsing import make_item_id, make_product_id, normalize_url
from .registry import ADAPTERS, detect_site_key, list_supported_sites


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _resolve_primary_listings(
    listings: list[PriceListing],
    *,
    source_url: str,
    canonical_url: str | None,
) -> list[PriceListing]:
    """Promote the listing that matches the requested product URL.

    This keeps the user-facing `amount` aligned with the URL they actually
    submitted when pages also expose alternate merchant offers.
    """
    if len(listings) < 2:
        return listings

    requested_refs = {
        normalize_url(candidate, keep_fragment=True)
        for candidate in (source_url, canonical_url)
        if candidate
    }
    if not requested_refs:
        return listings

    for index, listing in enumerate(listings):
        raw_listing_url = (listing.listing_url or "").strip()
        if not raw_listing_url:
            continue

        absolute_listing_url = urljoin(source_url, raw_listing_url)
        if normalize_url(absolute_listing_url, keep_fragment=True) not in requested_refs:
            continue
        if index == 0:
            return listings
        return [listing, *listings[:index], *listings[index + 1 :]]

    return listings


def fetch_price(
    url: str,
    timeout: int = 30,
    selected_sites: set[str] | None = None,
    fetch_mode: str = "auto",
) -> PriceResult:
    """Fetch and extract prices for a single URL.

    The function keeps the high-level policy in one place so API and other
    callers get identical behavior.
    """
    product_id = make_product_id(url)
    site_key = detect_site_key(url)
    adapter_key = site_key if site_key in ADAPTERS else "generic"
    extracted_at = _utc_now_iso()

    try:
        normalized_fetch_mode = parse_fetch_mode(fetch_mode)
    except ValueError as exc:
        return PriceResult(
            url=url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=str(exc),
            extracted_at=extracted_at,
        )

    if selected_sites is not None and adapter_key not in selected_sites:
        return PriceResult(
            url=url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=f"site_filtered_out:{adapter_key}",
            extracted_at=extracted_at,
        )

    modes_to_try = ["http"] if normalized_fetch_mode == "auto" else [normalized_fetch_mode]
    attempted_fetch_errors: list[str] = []
    fetched_any_page = False

    for mode in modes_to_try:
        try:
            page = fetch_page_with_mode(url, timeout=timeout, mode=mode)
        except Exception as exc:
            attempted_fetch_errors.append(f"{mode}: {exc}")
            if normalized_fetch_mode == "auto" and mode == "http":
                # If plain HTTP fails, queue browser modes as next attempts.
                modes_to_try.extend(["dynamic", "stealth"])
            continue

        fetched_any_page = True
        extracted = extract_price_from_page(
            page=page,
            url=url,
            selected_sites=selected_sites,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            extracted_at=extracted_at,
        )
        if extracted.success:
            return extracted

        if normalized_fetch_mode == "auto" and mode == "http":
            # HTTP succeeded but extraction failed; inspect signals for escalation.
            signals = detect_auto_escalation_signals(page)
            for followup_mode in auto_followup_modes(signals):
                if followup_mode not in modes_to_try:
                    modes_to_try.append(followup_mode)

    if fetched_any_page:
        return PriceResult(
            url=url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error="price_not_found",
            extracted_at=extracted_at,
        )

    error_detail = "; ".join(attempted_fetch_errors) if attempted_fetch_errors else "unknown"
    return PriceResult(
        url=url,
        success=False,
        product_id=product_id,
        site_key=site_key,
        adapter_key=adapter_key,
        error=f"fetch_failed: {error_detail}",
        extracted_at=extracted_at,
    )


def extract_price_from_page(
    page: Any,
    url: str,
    selected_sites: set[str] | None = None,
    *,
    product_id: str | None = None,
    site_key: str | None = None,
    adapter_key: str | None = None,
    extracted_at: str | None = None,
) -> PriceResult:
    """Extract a normalized `PriceResult` from an already-fetched page."""
    resolved_product_id = product_id or make_product_id(url)
    resolved_site_key = site_key or detect_site_key(url)
    resolved_adapter_key = adapter_key or (resolved_site_key if resolved_site_key in ADAPTERS else "generic")
    resolved_extracted_at = extracted_at or _utc_now_iso()
    metadata = extract_product_metadata(page, url)

    if selected_sites is not None and resolved_adapter_key not in selected_sites:
        return PriceResult(
            url=url,
            success=False,
            product_id=resolved_product_id,
            site_key=resolved_site_key,
            adapter_key=resolved_adapter_key,
            error=f"site_filtered_out:{resolved_adapter_key}",
            product_name=metadata["product_name"],
            brand=metadata["brand"],
            model=metadata["model"],
            sku=metadata["sku"],
            gtin=metadata["gtin"],
            image_url=metadata["image_url"],
            canonical_url=metadata["canonical_url"],
            availability=metadata["availability"],
            extracted_at=resolved_extracted_at,
        )

    adapter = ADAPTERS[resolved_adapter_key]
    candidates = adapter.extract(page, url)
    used_adapter = adapter.key

    if not candidates and adapter.key != "generic":
        generic_allowed = selected_sites is None or "generic" in selected_sites
        if generic_allowed:
            candidates = ADAPTERS["generic"].extract(page, url)
            used_adapter = "generic"

    if not candidates:
        return PriceResult(
            url=url,
            success=False,
            product_id=resolved_product_id,
            site_key=resolved_site_key,
            adapter_key=resolved_adapter_key,
            error="price_not_found",
            product_name=metadata["product_name"],
            brand=metadata["brand"],
            model=metadata["model"],
            sku=metadata["sku"],
            gtin=metadata["gtin"],
            image_url=metadata["image_url"],
            canonical_url=metadata["canonical_url"],
            availability=metadata["availability"],
            extracted_at=resolved_extracted_at,
        )

    listings = [
        PriceListing(
            item_id=make_item_id(url, candidate, index),
            amount=format(candidate.amount, "f"),
            currency=candidate.currency,
            raw_text=candidate.raw_text,
            source=candidate.source,
            seller=candidate.seller,
            listing_url=candidate.listing_url,
        )
        for index, candidate in enumerate(candidates, 1)
    ]
    listings = _resolve_primary_listings(
        listings,
        source_url=url,
        canonical_url=metadata["canonical_url"],
    )
    first = listings[0]
    return PriceResult(
        url=url,
        success=True,
        product_id=resolved_product_id,
        site_key=resolved_site_key,
        adapter_key=used_adapter,
        amount=first.amount,
        currency=first.currency,
        raw_text=first.raw_text,
        source=first.source,
        product_name=metadata["product_name"],
        brand=metadata["brand"],
        model=metadata["model"],
        sku=metadata["sku"],
        gtin=metadata["gtin"],
        image_url=metadata["image_url"],
        canonical_url=metadata["canonical_url"],
        availability=metadata["availability"],
        extracted_at=resolved_extracted_at,
        listing_count=len(listings),
        listings=listings,
    )


def scrape_prices(
    urls: Iterable[str],
    timeout: int = 30,
    selected_sites: set[str] | None = None,
    fetch_mode: str = "auto",
) -> list[PriceResult]:
    """Batch wrapper around `fetch_price` for in-process callers."""
    return [
        fetch_price(url, timeout=timeout, selected_sites=selected_sites, fetch_mode=fetch_mode)
        for url in urls
    ]


__all__ = [
    "SUPPORTED_FETCH_MODES",
    "extract_price_from_page",
    "fetch_price",
    "list_supported_sites",
    "scrape_prices",
]
