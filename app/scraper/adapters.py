from __future__ import annotations

"""Site-specific extraction adapters.

Adapters isolate target-specific selectors while reusing shared parsing helpers.
"""

import re

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
