from __future__ import annotations

"""Main scraper orchestration service.

This module is the application-facing entry point for scraping. It coordinates:
- URL/site resolution
- fetcher mode selection/escalation
- adapter execution
- final result shaping
"""

from collections.abc import Iterable

from .constants import SUPPORTED_FETCH_MODES
from .domain import PriceListing, PriceResult
from .fetching import auto_followup_modes, detect_auto_escalation_signals, fetch_page_with_mode
from .inputs import parse_fetch_mode
from .parsing import make_item_id, make_product_id
from .registry import ADAPTERS, detect_site_key, list_supported_sites


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
        )

    if selected_sites is not None and adapter_key not in selected_sites:
        return PriceResult(
            url=url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=f"site_filtered_out:{adapter_key}",
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
        adapter = ADAPTERS[adapter_key]
        candidates = adapter.extract(page, url)
        used_adapter = adapter.key

        if not candidates and adapter.key != "generic":
            # Preserve site-specific extraction first, then generic fallback.
            generic_allowed = selected_sites is None or "generic" in selected_sites
            if generic_allowed:
                candidates = ADAPTERS["generic"].extract(page, url)
                used_adapter = "generic"

        if candidates:
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
            first = listings[0]
            return PriceResult(
                url=url,
                success=True,
                product_id=product_id,
                site_key=site_key,
                adapter_key=used_adapter,
                amount=first.amount,
                currency=first.currency,
                raw_text=first.raw_text,
                source=first.source,
                listing_count=len(listings),
                listings=listings,
            )

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
        )

    error_detail = "; ".join(attempted_fetch_errors) if attempted_fetch_errors else "unknown"
    return PriceResult(
        url=url,
        success=False,
        product_id=product_id,
        site_key=site_key,
        adapter_key=adapter_key,
        error=f"fetch_failed: {error_detail}",
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
    "fetch_price",
    "list_supported_sites",
    "scrape_prices",
]
