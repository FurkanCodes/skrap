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
import logging
from typing import Any
from urllib.parse import urljoin

from .constants import SUPPORTED_FETCH_MODES
from .domain import PriceListing, PriceResult
from .fetching import auto_remaining_modes, detect_auto_escalation_signals, fetch_page_with_mode, initial_auto_fetch_mode
from .inputs import parse_fetch_mode
from .metadata import extract_product_metadata
from .parsing import make_item_id, make_product_id, normalize_runtime_url, normalize_url
from .registry import ADAPTERS, detect_site_key, list_supported_sites

REQUIRED_RESULT_FIELDS: tuple[str, ...] = ("amount", "product_name", "image_url")
_logger = logging.getLogger("scraper_api.scraper")
_PROXY_ERROR_HINTS = (
    "connect tunnel failed",
    "err_tunnel_connection_failed",
    "proxy",
)


def _classify_fetch_errors(errors: list[str]) -> str:
    if not errors:
        return "unknown"
    joined = " ".join(errors).lower()
    if any(hint in joined for hint in _PROXY_ERROR_HINTS):
        return "proxy_failed"
    if "timeout" in joined:
        return "timeout"
    if "connection" in joined or "network" in joined:
        return "network"
    return "fetch_failed"


def _classify_extraction_signals(signals: set[str]) -> str | None:
    if "amazon_captcha" in signals:
        return "blocked_by_amazon_captcha"
    if "cloudflare" in signals:
        return "blocked_by_cloudflare"
    if "protection" in signals:
        return "blocked_by_protection"
    return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _is_present(value: str | None) -> bool:
    return bool((value or "").strip())


def missing_required_result_fields(result: PriceResult) -> list[str]:
    """Return required field names missing from a scrape result."""
    missing: list[str] = []
    if not _is_present(result.amount):
        missing.append("amount")
    if not _is_present(result.product_name):
        missing.append("product_name")
    if not _is_present(result.image_url):
        missing.append("image_url")
    return missing


def is_result_qualified(result: PriceResult) -> bool:
    """Check if a result has required output fields for on-demand adapters."""
    return result.success and not missing_required_result_fields(result)


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
    runtime_url = normalize_runtime_url(url)
    product_id = make_product_id(runtime_url)
    site_key = detect_site_key(runtime_url)
    adapter_key = site_key if site_key in ADAPTERS else "generic"
    extracted_at = _utc_now_iso()

    try:
        normalized_fetch_mode = parse_fetch_mode(fetch_mode)
    except ValueError as exc:
        return PriceResult(
            url=runtime_url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=str(exc),
            extracted_at=extracted_at,
        )

    if selected_sites is not None and adapter_key not in selected_sites:
        return PriceResult(
            url=runtime_url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=f"site_filtered_out:{adapter_key}",
            extracted_at=extracted_at,
        )

    modes_to_try = [initial_auto_fetch_mode(runtime_url)] if normalized_fetch_mode == "auto" else [normalized_fetch_mode]
    attempted_fetch_errors: list[str] = []
    blocked_extract_error: str | None = None
    fetched_any_page = False
    _logger.info(
        "scrape_start url=%s fetch_mode=%s initial_modes=%s site=%s adapter=%s",
        runtime_url,
        normalized_fetch_mode,
        ",".join(modes_to_try),
        site_key,
        adapter_key,
    )

    for mode in modes_to_try:
        try:
            page = fetch_page_with_mode(runtime_url, timeout=timeout, mode=mode)
        except Exception as exc:
            attempted_fetch_errors.append(f"{mode}: {exc}")
            _logger.warning(
                "scrape_fetch_error mode=%s url=%s error=%s",
                mode,
                runtime_url,
                exc,
            )
            if normalized_fetch_mode == "auto":
                followup_modes = auto_remaining_modes(mode, url=runtime_url)
                for followup_mode in followup_modes:
                    if followup_mode not in modes_to_try:
                        modes_to_try.append(followup_mode)
                _logger.info(
                    "scrape_auto_escalate url=%s from_mode=%s to_modes=%s reason=fetch_error",
                    runtime_url,
                    mode,
                    ",".join(followup_modes) or "<none>",
                )
            continue

        fetched_any_page = True
        _logger.info("scrape_extract_start mode=%s url=%s adapter=%s", mode, runtime_url, adapter_key)
        extracted = extract_price_from_page(
            page=page,
            url=runtime_url,
            selected_sites=selected_sites,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            extracted_at=extracted_at,
        )
        if extracted.success:
            _logger.info(
                "scrape_success mode=%s url=%s adapter=%s amount=%s currency=%s source=%s",
                mode,
                runtime_url,
                extracted.adapter_key,
                extracted.amount,
                extracted.currency,
                extracted.source,
            )
            return extracted

        _logger.info(
            "scrape_extract_failed mode=%s url=%s adapter=%s error=%s",
            mode,
            runtime_url,
            extracted.adapter_key,
            extracted.error,
        )
        if normalized_fetch_mode == "auto":
            signals = detect_auto_escalation_signals(page)
            blocked_extract_error = blocked_extract_error or _classify_extraction_signals(signals)
            followup_modes = auto_remaining_modes(mode, signals=signals, url=runtime_url)
            for followup_mode in followup_modes:
                if followup_mode not in modes_to_try:
                    modes_to_try.append(followup_mode)
            _logger.info(
                "scrape_auto_escalate url=%s from_mode=%s to_modes=%s signals=%s reason=extract_failed",
                runtime_url,
                mode,
                ",".join(followup_modes) or "<none>",
                ",".join(sorted(signals)) or "<none>",
            )

    if fetched_any_page:
        error = blocked_extract_error or "price_not_found"
        _logger.info("scrape_failed url=%s error=%s attempted_modes=%s", runtime_url, error, ",".join(modes_to_try))
        return PriceResult(
            url=runtime_url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=error,
            extracted_at=extracted_at,
        )

    error_detail = "; ".join(attempted_fetch_errors) if attempted_fetch_errors else "unknown"
    error_class = _classify_fetch_errors(attempted_fetch_errors)
    _logger.info("scrape_failed url=%s error=%s attempted_modes=%s detail=%s", runtime_url, error_class, ",".join(modes_to_try), error_detail)
    return PriceResult(
        url=runtime_url,
        success=False,
        product_id=product_id,
        site_key=site_key,
        adapter_key=adapter_key,
        error=f"{error_class}: {error_detail}",
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
    "REQUIRED_RESULT_FIELDS",
    "SUPPORTED_FETCH_MODES",
    "extract_price_from_page",
    "fetch_price",
    "is_result_qualified",
    "list_supported_sites",
    "missing_required_result_fields",
    "scrape_prices",
]
