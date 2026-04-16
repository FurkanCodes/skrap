from __future__ import annotations

"""Generic extraction strategy shared across all sites.

Fallback order is intentionally strict to prioritize high-signal sources first:
1) structured data, 2) DOM selectors, 3) find/find_all, 4) metadata, 5) page text.
"""

import json
from typing import Any, Iterator

from scrapling.parser import Selector

from ..constants import (
    DOM_PRICE_SELECTORS,
    FIND_PRICE_ATTRIBUTE_FILTERS,
    FIND_TEXT_PRICE_REGEX,
    MAX_DOM_ELEMENTS,
    MAX_FIND_ELEMENTS,
    MAX_PAGE_TEXT_CANDIDATES,
    META_CURRENCY_SELECTORS,
    META_PRICE_SELECTORS,
    SKIPPED_FIND_REGEX_TAGS,
)
from ..domain import PriceCandidate
from ..parsing import normalize_currency, parse_decimal, scan_price_in_text, scan_prices_in_text


def _iter_json_nodes(value: Any) -> Iterator[dict[str, Any]]:
    """Yield every nested dict node from JSON-LD payloads."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_nodes(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_nodes(item)


def _candidate_from_price_data(data: dict[str, Any], source: str) -> PriceCandidate | None:
    """Build one candidate from schema-like price fields on a node."""
    direct_price = data.get("price")
    direct_currency = data.get("priceCurrency") or data.get("pricecurrency")

    seller_name: str | None = None
    seller = data.get("seller")
    if isinstance(seller, dict):
        raw_name = seller.get("name")
        if isinstance(raw_name, str):
            seller_name = raw_name
    elif isinstance(seller, str):
        seller_name = seller

    listing_url: str | None = None
    raw_url = data.get("url")
    if isinstance(raw_url, str):
        listing_url = raw_url

    if isinstance(direct_price, (str, int, float)):
        amount = parse_decimal(str(direct_price))
        if amount is not None:
            return PriceCandidate(
                raw_text=str(direct_price),
                amount=amount,
                currency=normalize_currency(str(direct_currency)) if direct_currency else None,
                source=source,
                seller=seller_name,
                listing_url=listing_url,
            )

    for key in ("lowPrice", "highPrice"):
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            amount = parse_decimal(str(value))
            if amount is not None:
                return PriceCandidate(
                    raw_text=str(value),
                    amount=amount,
                    currency=normalize_currency(str(direct_currency)) if direct_currency else None,
                    source=source,
                    seller=seller_name,
                    listing_url=listing_url,
                )

    return None


def _candidates_from_dict(data: dict[str, Any], source: str) -> list[PriceCandidate]:
    """Extract candidate list from a schema node, prioritizing explicit offers."""
    candidates: list[PriceCandidate] = []
    offers = data.get("offers")

    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                candidates.extend(_candidates_from_dict(offer, source))
    elif isinstance(offers, dict):
        candidates.extend(_candidates_from_dict(offers, source))

    # Explicit offers represent individual listings and should win over aggregate values.
    if candidates:
        return candidates

    direct = _candidate_from_price_data(data, source)
    return [direct] if direct else []


def deduplicate_candidates(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
    """Deduplicate by normalized economic identity fields."""
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[PriceCandidate] = []

    for candidate in candidates:
        key = (
            format(candidate.amount, "f"),
            candidate.currency or "",
            candidate.seller or "",
            candidate.listing_url or "",
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    return deduped


def _element_text_candidates(element: Selector) -> tuple[str, str, str, str, str]:
    """Collect text/attribute values once so scanners can reuse them."""
    return (
        str(element.get_all_text(separator=" ", strip=True)),
        str(element.attrib.get("content", "")),
        str(element.attrib.get("data-price", "")),
        str(element.attrib.get("value", "")),
        str(element.attrib.get("aria-label", "")),
    )


def _candidate_from_element(
    element: Selector,
    source: str,
    meta_currency: str | None,
    require_currency: bool,
    text_candidates: tuple[str, str, str, str, str] | None = None,
) -> PriceCandidate | None:
    """Parse one candidate from a selected element using pre-collected text fields."""
    listing_url = str(element.attrib.get("href", "")).strip() or None
    values = text_candidates or _element_text_candidates(element)

    for text in values:
        if not text:
            continue
        parsed = scan_price_in_text(text, source=source, require_currency=require_currency)
        if not parsed:
            continue
        return PriceCandidate(
            raw_text=parsed.raw_text,
            amount=parsed.amount,
            currency=parsed.currency or meta_currency,
            source=source,
            listing_url=listing_url,
        )

    return None


def _extract_find_based_candidates(page: Selector, meta_currency: str | None) -> list[PriceCandidate]:
    """Fallback extraction using Scrapling `find/find_all` filters and regex."""
    candidates: list[PriceCandidate] = []

    for index, filter_map in enumerate(FIND_PRICE_ATTRIBUTE_FILTERS):
        source = f"find:filter:{index}"
        elements = page.find_all(filter_map)[:MAX_FIND_ELEMENTS]
        if elements:
            first_element = elements[0]
            first_values = _element_text_candidates(first_element)
            first_candidate = _candidate_from_element(
                first_element,
                source=f"{source}:first",
                meta_currency=meta_currency,
                require_currency=False,
                text_candidates=first_values,
            )
            if first_candidate:
                candidates.append(first_candidate)

        for element in elements:
            values = _element_text_candidates(element)
            candidate = _candidate_from_element(
                element,
                source=source,
                meta_currency=meta_currency,
                require_currency=False,
                text_candidates=values,
            )
            if candidate:
                candidates.append(candidate)

    regex_elements = page.find_all(FIND_TEXT_PRICE_REGEX)[:MAX_FIND_ELEMENTS]
    if regex_elements:
        first_values = _element_text_candidates(regex_elements[0])
        first_candidate = _candidate_from_element(
            regex_elements[0],
            source="find:regex:first",
            meta_currency=meta_currency,
            require_currency=True,
            text_candidates=first_values,
        )
        if first_candidate:
            candidates.append(first_candidate)

    for element in regex_elements:
        if str(getattr(element, "tag", "")).lower() in SKIPPED_FIND_REGEX_TAGS:
            continue
        values = _element_text_candidates(element)
        candidate = _candidate_from_element(
            element,
            source="find:regex",
            meta_currency=meta_currency,
            require_currency=True,
            text_candidates=values,
        )
        if candidate:
            candidates.append(candidate)

    return deduplicate_candidates(candidates)


def extract_price_listings_from_selector(page: Selector) -> list[PriceCandidate]:
    """Extract all price listings from a parsed page.

    This function is the canonical generic strategy used directly by the generic
    adapter and indirectly by site adapters as a fallback.
    """
    # Resolve a page-level currency hint once, then propagate it downstream.
    meta_currency: str | None = None
    for selector in META_CURRENCY_SELECTORS:
        value = page.css(selector).get()
        if value:
            meta_currency = normalize_currency(str(value))
            if meta_currency:
                break

    # 1) Structured data is usually the most reliable on multi-offer pages.
    json_candidates: list[PriceCandidate] = []
    for idx, json_blob in enumerate(page.css("script[type='application/ld+json']::text").getall()):
        blob_text = str(json_blob).strip()
        if not blob_text:
            continue
        try:
            payload = json.loads(blob_text)
        except json.JSONDecodeError:
            continue

        for node in _iter_json_nodes(payload):
            json_candidates.extend(_candidates_from_dict(node, source=f"json-ld[{idx}]"))

    if json_candidates:
        return deduplicate_candidates(json_candidates)

    # 2) DOM selectors capture common rendered price widgets.
    dom_candidates: list[PriceCandidate] = []
    for selector in DOM_PRICE_SELECTORS:
        for element in page.css(selector)[:MAX_DOM_ELEMENTS]:
            text_candidates = _element_text_candidates(element)
            listing_url = str(element.attrib.get("href", "")).strip() or None

            for text in text_candidates:
                if not text:
                    continue
                candidate = scan_price_in_text(text, source=f"dom:{selector}", require_currency=False)
                if candidate:
                    dom_candidates.append(
                        PriceCandidate(
                            raw_text=candidate.raw_text,
                            amount=candidate.amount,
                            currency=candidate.currency or meta_currency,
                            source=candidate.source,
                            listing_url=listing_url,
                        )
                    )
                    break

    if dom_candidates:
        return deduplicate_candidates(dom_candidates)

    # 3) Filter-based find/find_all fallback for weaker or shifting markup.
    find_candidates = _extract_find_based_candidates(page, meta_currency=meta_currency)
    if find_candidates:
        return find_candidates

    # 4) Metadata fallback for pages exposing only meta prices.
    for selector, source in META_PRICE_SELECTORS:
        value = page.css(selector).get()
        if not value:
            continue
        candidate = scan_price_in_text(str(value), source=source, require_currency=False)
        if candidate:
            if candidate.currency or not meta_currency:
                return [candidate]
            return [
                PriceCandidate(
                    raw_text=candidate.raw_text,
                    amount=candidate.amount,
                    currency=meta_currency,
                    source=source,
                )
            ]

    # 5) Last resort: full-page scan requiring currency tokens to reduce noise.
    all_text = str(page.get_all_text(separator=" ", strip=True))
    fallback_candidates = scan_prices_in_text(all_text, source="page-text", require_currency=True)[
        :MAX_PAGE_TEXT_CANDIDATES
    ]
    normalized_fallback = [
        PriceCandidate(
            raw_text=candidate.raw_text,
            amount=candidate.amount,
            currency=candidate.currency or meta_currency,
            source=candidate.source,
            seller=candidate.seller,
            listing_url=candidate.listing_url,
        )
        for candidate in fallback_candidates
    ]
    return deduplicate_candidates(normalized_fallback)


def extract_price_from_selector(page: Selector) -> PriceCandidate | None:
    """Convenience accessor for callers that only need the first candidate."""
    listings = extract_price_listings_from_selector(page)
    return listings[0] if listings else None
