from __future__ import annotations

"""Site-specific extraction adapters.

Adapters isolate target-specific selectors while reusing shared parsing helpers.
"""

import re
from urllib.parse import parse_qs, urlparse
import json

from scrapling.parser import Selector

from .domain import PriceCandidate
from .extractors.generic import deduplicate_candidates, extract_price_listings_from_selector
from .parsing import scan_price_in_text


class SiteAdapter:
    """Base adapter contract used by the registry/service layers."""

    key = "generic"

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        raise NotImplementedError


class GenericAdapter(SiteAdapter):
    """Default adapter for unknown domains."""

    key = "generic"

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        return extract_price_listings_from_selector(page)


class AkakceAdapter(SiteAdapter):
    """Akakce-specific listing parser with seller cleanup rules."""

    key = "akakce"

    @staticmethod
    def _normalize_price_text(text: str) -> str:
        compact = " ".join(text.split())
        return re.sub(r"\s+([,.]\d{2}\b)", r"\1", compact)

    @staticmethod
    def _extract_seller(li: Selector) -> str | None:
        seller_nodes = li.css(".v_v8")
        if not seller_nodes:
            return None

        seller_node = seller_nodes[0]
        seller_text = str(seller_node.get_all_text(separator=" ", strip=True))
        seller_text = seller_text.lstrip("/").strip()
        seller_text = re.sub(r"\d+[,.]\d+\s+\d+\s+Yorum.*$", "", seller_text, flags=re.IGNORECASE).strip()
        seller_text = re.sub(r"\d+\s+Yorum.*$", "", seller_text, flags=re.IGNORECASE).strip()
        if seller_text:
            return seller_text

        img_alt = seller_node.css("img::attr(alt)").get()
        if img_alt:
            fallback = str(img_alt).strip()
            if fallback:
                return fallback
        return None

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        candidates: list[PriceCandidate] = []
        for li in page.css("#PL > li"):
            price_nodes = li.css(".pt_v8")
            if not price_nodes:
                continue

            price_text = self._normalize_price_text(str(price_nodes[0].get_all_text(separator=" ", strip=True)))
            candidate = scan_price_in_text(price_text, source="akakce:dom", require_currency=False)
            if not candidate:
                continue

            seller_text = self._extract_seller(li)
            href = li.css("a::attr(href)").get()
            listing_url = str(href).strip() if href else None

            candidates.append(
                PriceCandidate(
                    raw_text=candidate.raw_text,
                    amount=candidate.amount,
                    currency=candidate.currency or "TRY",
                    source="akakce:dom",
                    seller=seller_text,
                    listing_url=listing_url,
                )
            )

        if candidates:
            return deduplicate_candidates(candidates)
        return extract_price_listings_from_selector(page)


class NeweggAdapter(SiteAdapter):
    """Newegg parser for both listing and product-detail pages."""

    key = "newegg"

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        candidates: list[PriceCandidate] = []
        items = page.css(".item-cell")
        if items:
            for item in items:
                price_nodes = item.css(".price-current")
                if not price_nodes:
                    continue

                price_text = str(price_nodes[0].get_all_text(separator=" ", strip=True))
                candidate = scan_price_in_text(price_text, source="newegg:listing", require_currency=False)
                if not candidate:
                    continue

                listing_url = item.css("a.item-title::attr(href)").get()
                title = item.css("a.item-title::text").get()
                seller = str(title).strip() if title else None
                candidates.append(
                    PriceCandidate(
                        raw_text=candidate.raw_text,
                        amount=candidate.amount,
                        currency=candidate.currency,
                        source="newegg:listing",
                        seller=seller,
                        listing_url=str(listing_url).strip() if listing_url else None,
                    )
                )

            if candidates:
                return deduplicate_candidates(candidates)

        # Product-detail fallback when no listing grid is available.
        price_nodes = page.css(".price-current")
        if price_nodes:
            price_text = str(price_nodes[0].get_all_text(separator=" ", strip=True))
            candidate = scan_price_in_text(price_text, source="newegg:product", require_currency=False)
            if candidate:
                seller_text = page.css(".product-seller-name::text").get()
                return [
                    PriceCandidate(
                        raw_text=candidate.raw_text,
                        amount=candidate.amount,
                        currency=candidate.currency,
                        source="newegg:product",
                        seller=str(seller_text).strip() if seller_text else None,
                        listing_url=url,
                    )
                ]

        return extract_price_listings_from_selector(page)


class N11Adapter(SiteAdapter):
    """n11 parser favoring selected seller price over generic aggregate offers."""

    key = "n11"

    @staticmethod
    def _extract_json_object_after_marker(script_text: str, marker: str) -> dict | None:
        marker_index = script_text.find(marker)
        if marker_index < 0:
            return None

        start = script_text.find("{", marker_index)
        if start < 0:
            return None

        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(script_text)):
            char = script_text[index]
            if in_string:
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
                continue
            if char == "}":
                depth -= 1
                if depth == 0:
                    chunk = script_text[start : index + 1]
                    try:
                        parsed = json.loads(chunk)
                    except json.JSONDecodeError:
                        return None
                    if isinstance(parsed, dict):
                        return parsed
                    return None
        return None

    @classmethod
    def _find_key(cls, payload: object, key: str) -> object | None:
        if isinstance(payload, dict):
            if key in payload:
                return payload[key]
            for value in payload.values():
                found = cls._find_key(value, key)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = cls._find_key(item, key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _seller_from_url(url: str | None) -> str | None:
        if not url:
            return None
        query = parse_qs(urlparse(url).query)
        for key in ("magaza", "seller", "merchant"):
            values = query.get(key)
            if values and values[0].strip():
                return values[0].strip().lower()
        return None

    @classmethod
    def _prefer_requested_seller(
        cls,
        candidates: list[PriceCandidate],
        *,
        request_url: str,
    ) -> list[PriceCandidate]:
        if len(candidates) < 2:
            return candidates

        preferred_seller = cls._seller_from_url(request_url)
        if not preferred_seller:
            return candidates

        for index, candidate in enumerate(candidates):
            candidate_seller = cls._seller_from_url(candidate.listing_url)
            if candidate_seller != preferred_seller:
                continue
            if index == 0:
                return candidates
            return [candidate, *candidates[:index], *candidates[index + 1 :]]
        return candidates

    def _extract_from_initial_state(self, page: Selector, url: str) -> list[PriceCandidate]:
        html = str(getattr(page, "html_content", "") or "")
        if "__INITIAL_STATE__" not in html:
            return []

        payload = self._extract_json_object_after_marker(html, "__INITIAL_STATE__")
        if not payload:
            return []

        raw_offers = self._find_key(payload, "unifiedProductList")
        if not isinstance(raw_offers, list):
            return []

        candidates: list[PriceCandidate] = []
        for offer in raw_offers:
            if not isinstance(offer, dict):
                continue

            raw_price = offer.get("price")
            listing_url = offer.get("url")
            seller_name = offer.get("sellerNickName")
            parsed = scan_price_in_text(str(raw_price or ""), source="n11:initial-state", require_currency=False)
            if not parsed:
                continue
            candidates.append(
                PriceCandidate(
                    raw_text=parsed.raw_text,
                    amount=parsed.amount,
                    currency=parsed.currency or "TRY",
                    source="n11:initial-state",
                    seller=str(seller_name).strip() if isinstance(seller_name, str) else None,
                    listing_url=str(listing_url).strip() if isinstance(listing_url, str) else None,
                )
            )

        if candidates:
            ordered = self._prefer_requested_seller(candidates, request_url=url)
            return deduplicate_candidates(ordered)

        return []

    def _extract_selected_offer(self, page: Selector, url: str) -> PriceCandidate | None:
        html = str(getattr(page, "html_content", "") or "")
        if not html:
            return None

        product_meta = self._extract_json_object_after_marker(html, '"productMeta":')
        if not isinstance(product_meta, dict):
            return None

        seller_text = page.css(".sellerInfo .sellerNameWrapper a::text").get() or page.css(".sellerInfo a::text").get()
        raw_price = product_meta.get("price") or product_meta.get("discountedPrice")
        parsed = scan_price_in_text(str(raw_price or ""), source="n11:selected-meta", require_currency=False)
        if not parsed:
            return None

        return PriceCandidate(
            raw_text=parsed.raw_text,
            amount=parsed.amount,
            currency=parsed.currency or "TRY",
            source="n11:selected-meta",
            seller=str(seller_text).strip() if seller_text else None,
            listing_url=url,
        )

    def _extract_from_dom(self, page: Selector, url: str) -> list[PriceCandidate]:
        candidates: list[PriceCandidate] = []
        for row in page.css(".unifiedProduct"):
            raw_price = row.css(".priceDisplay::text").get()
            if not raw_price:
                continue

            parsed = scan_price_in_text(str(raw_price), source="n11:dom", require_currency=False)
            if not parsed:
                continue

            listing_url = row.css("a[href*='?magaza=']::attr(href)").get()
            seller = row.css(".title .name::text").get()
            candidates.append(
                PriceCandidate(
                    raw_text=parsed.raw_text,
                    amount=parsed.amount,
                    currency=parsed.currency or "TRY",
                    source="n11:dom",
                    seller=str(seller).strip() if seller else None,
                    listing_url=str(listing_url).strip() if listing_url else None,
                )
            )

        if candidates:
            ordered = self._prefer_requested_seller(candidates, request_url=url)
            return deduplicate_candidates(ordered)
        return []

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        selected_offer = self._extract_selected_offer(page, url)
        initial_state_candidates = self._extract_from_initial_state(page, url)
        if initial_state_candidates:
            if selected_offer is not None:
                return deduplicate_candidates([selected_offer, *initial_state_candidates])
            return initial_state_candidates

        dom_candidates = self._extract_from_dom(page, url)
        if dom_candidates:
            if selected_offer is not None:
                return deduplicate_candidates([selected_offer, *dom_candidates])
            return dom_candidates

        if selected_offer is not None:
            return [selected_offer]

        return extract_price_listings_from_selector(page)
