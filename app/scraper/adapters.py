from __future__ import annotations

"""Site-specific extraction adapters.

Adapters isolate target-specific selectors while reusing shared parsing helpers.
"""

import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from scrapling.parser import Selector

from .availability import normalize_availability
from .domain import PriceCandidate
from .extractors.generic import deduplicate_candidates, extract_price_listings_from_selector
from .parsing import normalize_currency, parse_decimal, scan_price_in_text


def _extract_json_object_after_marker(script_text: str, marker: str) -> dict[str, Any] | None:
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


def _candidate_signals(
    *signals: str,
    seller: str | None = None,
    listing_url: str | None = None,
    currency: str | None = None,
) -> tuple[str, ...]:
    values = [signal for signal in signals if signal]
    if seller:
        values.append("seller")
    if listing_url:
        values.append("listing_url")
    if currency:
        values.append("currency")
    return tuple(dict.fromkeys(values))


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


def _extract_candidates_from_selectors(
    page: Selector,
    *,
    selectors: tuple[str, ...],
    source_prefix: str,
    default_currency: str | None = None,
) -> list[PriceCandidate]:
    candidates: list[PriceCandidate] = []
    for index, selector in enumerate(selectors):
        for raw_value in page.css(selector).getall():
            text = " ".join(str(raw_value).split()).strip()
            if not text:
                continue
            parsed = scan_price_in_text(
                text,
                source=f"{source_prefix}:selector:{index}",
                require_currency=False,
            )
            if not parsed:
                continue
            candidates.append(
                PriceCandidate(
                    raw_text=parsed.raw_text,
                    amount=parsed.amount,
                    currency=parsed.currency or default_currency,
                    source=f"{source_prefix}:selector:{index}",
                    confidence="high",
                    signals=_candidate_signals(
                        "site_selector",
                        currency=parsed.currency or default_currency,
                    ),
                )
            )
    return deduplicate_candidates(candidates)


class ConfiguredSiteAdapter(SiteAdapter):
    """Selector-driven adapter for websites with predictable price nodes."""

    def __init__(
        self,
        *,
        key: str,
        price_selectors: tuple[str, ...],
        default_currency: str | None = None,
    ) -> None:
        self.key = key
        self._price_selectors = price_selectors
        self._default_currency = default_currency

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        candidates = _extract_candidates_from_selectors(
            page,
            selectors=self._price_selectors,
            source_prefix=self.key,
            default_currency=self._default_currency,
        )
        if candidates:
            return candidates
        return extract_price_listings_from_selector(page)


class AmazonAdapter(SiteAdapter):
    """Amazon parser constrained to buy-box/product scope.

    Amazon pages often include unrelated merchandising prices. We intentionally
    avoid generic fallback so only PDP/buy-box prices are returned.
    """

    key = "amazon"

    _price_selectors = (
        "#corePrice_feature_div .a-price .a-offscreen::text",
        "#corePrice_feature_div .a-offscreen::text",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen::text",
        "#corePriceDisplay_desktop_feature_div .a-offscreen::text",
        "#corePriceDisplay_mobile_feature_div .a-price .a-offscreen::text",
        "#corePriceDisplay_mobile_feature_div .a-offscreen::text",
        "#priceblock_ourprice::text",
        "#priceblock_dealprice::text",
        "#kindle-price::text",
    )
    _container_selectors = (
        "#corePrice_feature_div",
        "#corePriceDisplay_desktop_feature_div",
        "#corePriceDisplay_mobile_feature_div",
        "#apex_offerDisplay_desktop",
        "#apex_offerDisplay_single_desktop",
        "#apex_desktop",
        "#tp_price_block_total_price_ww",
        "#buyBoxAccordion",
        "#desktop_buybox",
    )
    _exact_scoped_selectors = (
        ".apex-pricetopay-value .a-offscreen::text",
        ".reinventPricePriceToPayMargin .a-offscreen::text",
        ".priceToPay .a-offscreen::text",
        "#apex-pricetopay-accessibility-label::text",
        "[data-pricetopay-label]::attr(data-pricetopay-label)",
        "#tp_price_block_total_price_ww .a-offscreen::text",
        "#priceblock_ourprice::text",
        "#priceblock_dealprice::text",
        "#kindle-price::text",
    )
    _scoped_element_selectors = (
        ".apex-pricetopay-value",
        ".reinventPricePriceToPayMargin",
        ".priceToPay",
        "#tp_price_block_total_price_ww",
        ".a-price",
        "[id*='priceblock']",
        "[class*='priceToPay']",
        "[class*='apexPrice']",
    )
    _negative_price_hints = (
        "installment",
        "installments",
        "taksit",
        "ayda",
        "/ay",
        "coupon",
        "kupon",
        "you save",
        "save ",
        "tasarruf",
        "list price",
        "liste fiyati",
        "liste fiyat",
    )
    _negative_marker_hints = (
        "basisprice",
        "coupon",
        "savings",
        "installment",
        "listprice",
        "priceperunit",
        "secondary",
        "strike",
        "text-price",
        "legalmessage",
    )
    _generic_price_selectors = {".a-price"}
    _positive_price_context_hints = (
        "apex",
        "coreprice",
        "priceblock",
        "pricetopay",
    )
    _hidden_display_price_selectors = (
        'input[name*="customerVisiblePrice"][name$="[displayString]"]::attr(value)',
        'input[id*="customerVisiblePrice"][id$="[displayString]"]::attr(value)',
    )
    _hidden_amount_price_selectors = (
        'input[name*="customerVisiblePrice"][name$="[amount]"]::attr(value)',
        'input[id*="customerVisiblePrice"][id$="[amount]"]::attr(value)',
    )
    _hidden_currency_selectors = (
        'input[name*="customerVisiblePrice"][name$="[currencyCode]"]::attr(value)',
        'input[id*="customerVisiblePrice"][id$="[currencyCode]"]::attr(value)',
    )
    _twister_json_selectors = (
        ".twister-plus-buying-options-price-data::text",
    )

    def _is_unavailable(self, page: Selector) -> bool:
        checks: list[str | None] = [
            page.css("#availability span::text").get(),
            page.css("#availabilityInsideBuyBox_feature_div #availability span::text").get(),
            page.css("#availabilityOutsideBuyBox_feature_div #availability span::text").get(),
            page.css("#outOfStock span::text").get(),
            page.css("#outOfStock::text").get(),
            page.css("#outOfStock_feature_div span::text").get(),
        ]
        for value in checks:
            if normalize_availability(value) == "out_of_stock":
                return True

        page_text = " ".join(str(page.get_all_text(separator=" ", strip=True)).split())
        return normalize_availability(page_text) == "out_of_stock"

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.split()).strip()

    def _is_negative_text(self, text: str) -> bool:
        normalized = self._normalize_text(text).lower()
        return any(hint in normalized for hint in self._negative_price_hints)

    @staticmethod
    def _marker_text(element: Selector) -> str:
        markers = []
        for key, value in element.attrib.items():
            markers.append(str(key))
            markers.append(str(value))
        return " ".join(markers).lower()

    def _is_negative_marker(self, element: Selector) -> bool:
        markers = self._marker_text(element)
        return any(hint in markers for hint in self._negative_marker_hints)

    def _has_negative_context(self, element: Selector) -> bool:
        current: Selector | None = element
        while current is not None:
            if self._is_negative_marker(current):
                return True
            current = current.parent
        return False

    def _has_positive_price_context(self, element: Selector) -> bool:
        current: Selector | None = element
        while current is not None:
            markers = self._marker_text(current)
            if any(hint in markers for hint in self._positive_price_context_hints):
                return True
            current = current.parent
        return False

    def _candidate_from_text(self, text: str, source: str) -> PriceCandidate | None:
        normalized_text = self._normalize_text(text)
        if not normalized_text or self._is_negative_text(normalized_text):
            return None
        parsed = scan_price_in_text(
            normalized_text,
            source=source,
            require_currency=False,
        )
        if not parsed:
            return None
        return PriceCandidate(
            raw_text=parsed.raw_text,
            amount=parsed.amount,
            currency=parsed.currency,
            source=source,
            confidence="high",
            signals=_candidate_signals(
                "site_selector",
                "amazon_scoped",
                currency=parsed.currency,
            ),
        )

    def _candidate_from_amount_currency(
        self,
        amount_text: str,
        currency_text: str | None,
        *,
        raw_text: str | None,
        source: str,
        extra_signal: str,
    ) -> PriceCandidate | None:
        normalized_amount = self._normalize_text(amount_text)
        amount = parse_decimal(normalized_amount)
        if amount is None:
            return None

        normalized_currency = normalize_currency(currency_text)
        normalized_raw = self._normalize_text(raw_text or "")
        if not normalized_raw:
            normalized_raw = self._normalize_text(
                f"{normalized_amount} {currency_text or ''}"
            )
        if not normalized_raw or self._is_negative_text(normalized_raw):
            return None

        return PriceCandidate(
            raw_text=normalized_raw,
            amount=amount,
            currency=normalized_currency,
            source=source,
            confidence="high",
            signals=_candidate_signals(
                "embedded_state",
                extra_signal,
                currency=normalized_currency,
            ),
        )

    @staticmethod
    def _first_clean(values: list[Any]) -> str | None:
        for value in values:
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
        return None

    @classmethod
    def _iter_json_price_nodes(cls, value: Any) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        if isinstance(value, dict):
            if any(
                key in value
                for key in ("displayPrice", "priceAmount", "currencySymbol", "currencyCode")
            ):
                nodes.append(value)
            for child in value.values():
                nodes.extend(cls._iter_json_price_nodes(child))
        elif isinstance(value, list):
            for item in value:
                nodes.extend(cls._iter_json_price_nodes(item))
        return nodes

    def _extract_hidden_buybox_candidates(self, page: Selector) -> list[PriceCandidate]:
        candidates: list[PriceCandidate] = []

        for selector_index, selector in enumerate(self._hidden_display_price_selectors):
            for raw_value in page.css(selector).getall():
                candidate = self._candidate_from_text(
                    str(raw_value),
                    source=f"{self.key}:hidden-buybox-display:{selector_index}",
                )
                if candidate is not None:
                    candidates.append(candidate)
        if candidates:
            return deduplicate_candidates(candidates)

        currency = self._first_clean(
            [
                value
                for selector in self._hidden_currency_selectors
                for value in page.css(selector).getall()
            ]
        )
        for selector_index, selector in enumerate(self._hidden_amount_price_selectors):
            for raw_value in page.css(selector).getall():
                candidate = self._candidate_from_amount_currency(
                    str(raw_value),
                    currency,
                    raw_text=None,
                    source=f"{self.key}:hidden-buybox-amount:{selector_index}",
                    extra_signal="amazon_buybox",
                )
                if candidate is not None:
                    candidates.append(candidate)

        twister_amount = page.css("#twister-plus-price-data-price::attr(value)").get()
        twister_currency = page.css("#twister-plus-price-data-price-unit::attr(value)").get()
        if twister_amount:
            candidate = self._candidate_from_amount_currency(
                str(twister_amount),
                str(twister_currency) if twister_currency else None,
                raw_text=None,
                source=f"{self.key}:twister-hidden",
                extra_signal="amazon_twister",
            )
            if candidate is not None:
                candidates.append(candidate)

        return deduplicate_candidates(candidates)

    def _extract_twister_json_candidates(self, page: Selector) -> list[PriceCandidate]:
        candidates: list[PriceCandidate] = []
        for selector_index, selector in enumerate(self._twister_json_selectors):
            for raw_blob in page.css(selector).getall():
                blob = str(raw_blob).strip()
                if not blob:
                    continue
                try:
                    payload = json.loads(blob)
                except json.JSONDecodeError:
                    continue

                for node in self._iter_json_price_nodes(payload):
                    display_price = node.get("displayPrice")
                    if isinstance(display_price, str) and display_price.strip():
                        candidate = self._candidate_from_text(
                            display_price,
                            source=f"{self.key}:twister-json:{selector_index}",
                        )
                        if candidate is not None:
                            candidates.append(candidate)
                            continue

                    amount = node.get("priceAmount")
                    currency = node.get("currencyCode") or node.get("currencySymbol") or node.get("currency")
                    if isinstance(amount, (int, float, str)):
                        candidate = self._candidate_from_amount_currency(
                            str(amount),
                            str(currency) if currency is not None else None,
                            raw_text=None,
                            source=f"{self.key}:twister-json:{selector_index}",
                            extra_signal="amazon_twister",
                        )
                        if candidate is not None:
                            candidates.append(candidate)

        return deduplicate_candidates(candidates)

    def _extract_ranked_candidates_from_container(
        self,
        container: Selector,
        *,
        container_index: int,
    ) -> list[tuple[tuple[int, int, int], PriceCandidate]]:
        ranked: list[tuple[tuple[int, int, int], PriceCandidate]] = []

        for selector_index, selector in enumerate(self._exact_scoped_selectors):
            for text_index, raw_value in enumerate(container.css(selector).getall()):
                candidate = self._candidate_from_text(
                    str(raw_value),
                    source=f"{self.key}:scoped-exact:{container_index}:{selector_index}",
                )
                if candidate is None:
                    continue
                ranked.append(((0, selector_index, text_index), candidate))

        for selector_index, selector in enumerate(self._scoped_element_selectors):
            for element_index, element in enumerate(container.css(selector)):
                if self._has_negative_context(element):
                    continue
                if selector in self._generic_price_selectors and not self._has_positive_price_context(element):
                    continue
                candidate = self._candidate_from_text(
                    str(element.get_all_text(separator=" ", strip=True)),
                    source=f"{self.key}:scoped-fallback:{container_index}:{selector_index}",
                )
                if candidate is None:
                    continue
                ranked.append(((1, selector_index, element_index), candidate))

        return ranked

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        if self._is_unavailable(page):
            return []
        hidden_candidates = self._extract_hidden_buybox_candidates(page)
        if hidden_candidates:
            return hidden_candidates
        twister_candidates = self._extract_twister_json_candidates(page)
        if twister_candidates:
            return twister_candidates

        ranked_candidates: list[tuple[tuple[int, int, int], PriceCandidate]] = []
        for container_index, selector in enumerate(self._container_selectors):
            for container in page.css(selector):
                ranked_candidates.extend(
                    self._extract_ranked_candidates_from_container(
                        container,
                        container_index=container_index,
                    )
                )
        if ranked_candidates:
            ranked_candidates.sort(key=lambda item: item[0])
            return deduplicate_candidates([candidate for _, candidate in ranked_candidates])

        return _extract_candidates_from_selectors(
            page,
            selectors=self._price_selectors,
            source_prefix=self.key,
        )


class TeknosaAdapter(SiteAdapter):
    """Teknosa parser that avoids hidden mini-cart zero totals."""

    key = "teknosa"

    _product_price_selectors = (
        "#visiblePi::attr(value)",
        ".pdp-cart-price .prc::text",
        ".pdp-amount .prc::text",
    )
    _fallback_price_selectors = (
        "[itemprop='price']::attr(content)",
        "meta[property='product:price:amount']::attr(content)",
        "[data-testid*='price']::text",
        "[class*='price']::text",
        "[id*='price']::text",
    )

    @staticmethod
    def _positive_only(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
        return [candidate for candidate in candidates if candidate.amount > 0]

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        product_candidates = _extract_candidates_from_selectors(
            page,
            selectors=self._product_price_selectors,
            source_prefix=self.key,
            default_currency="TRY",
        )
        positive_product_candidates = self._positive_only(product_candidates)
        if positive_product_candidates:
            return positive_product_candidates

        fallback_candidates = _extract_candidates_from_selectors(
            page,
            selectors=self._fallback_price_selectors,
            source_prefix=self.key,
            default_currency="TRY",
        )
        positive_fallback_candidates = self._positive_only(fallback_candidates)
        if positive_fallback_candidates:
            return positive_fallback_candidates

        generic_candidates = extract_price_listings_from_selector(page)
        return self._positive_only(generic_candidates)


class HepsiburadaAdapter(SiteAdapter):
    """Hepsiburada parser that flags basket-only discounted prices."""

    key = "hepsiburada"

    _cart_special_pattern = re.compile(
        r"(?P<label>sepete\s+özel\s+fiyat)\s+"
        r"(?P<amount>(?:\d{1,3}(?:[\s.]\d{3})+|\d+)(?:,\d{2})?)\s*"
        r"(?P<currency>tl|try|₺)?",
        re.IGNORECASE,
    )
    _price_selectors = (
        "[data-test-id='price-current-price']::text",
        "[itemprop='price']::attr(content)",
        ".price::text",
    )

    def _extract_cart_special_price(self, page: Selector) -> PriceCandidate | None:
        page_text = str(page.get_all_text(separator=" ", strip=True))
        compact = " ".join(page_text.split())
        match = self._cart_special_pattern.search(compact)
        if not match:
            return None

        currency_text = match.group("currency") or "TL"
        price_text = f"{match.group('amount')} {currency_text}"
        parsed = scan_price_in_text(price_text, source="hepsiburada:cart-special", require_currency=False)
        if not parsed:
            return None

        return PriceCandidate(
            raw_text=f"{match.group('label')} {parsed.raw_text}",
            amount=parsed.amount,
            currency=parsed.currency or "TRY",
            source="hepsiburada:cart-special",
            confidence="high",
            signals=_candidate_signals(
                "site_text_pattern",
                "cart_special",
                currency=parsed.currency or "TRY",
            ),
        )

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        cart_special = self._extract_cart_special_price(page)
        if cart_special is not None:
            return [cart_special]

        selector_candidates = _extract_candidates_from_selectors(
            page,
            selectors=self._price_selectors,
            source_prefix=self.key,
            default_currency="TRY",
        )
        if selector_candidates:
            return selector_candidates

        return extract_price_listings_from_selector(page)


class TrendyolAdapter(SiteAdapter):
    """Trendyol parser that prefers PDP winner-variant state over stale JSON-LD."""

    key = "trendyol"

    _price_selectors = (
        ".prc-dsc::text",
        ".prc-slg::text",
        "[itemprop='price']::attr(content)",
    )
    _price_keys = (
        "discountedPrice",
        "discountedPriceAfterNoLimitPromotions",
        "sellingPrice",
        "originalPrice",
    )

    @staticmethod
    def _price_text(price_data: dict[str, Any]) -> tuple[str, str | None] | None:
        currency = price_data.get("currency")
        normalized_currency = str(currency).strip() if isinstance(currency, str) and currency.strip() else None

        for key in TrendyolAdapter._price_keys:
            raw_price = price_data.get(key)
            if not isinstance(raw_price, dict):
                continue

            raw_text = raw_price.get("text")
            if isinstance(raw_text, str) and raw_text.strip() and "nan" not in raw_text.lower():
                return raw_text.strip(), normalized_currency

            raw_value = raw_price.get("value")
            if isinstance(raw_value, (int, float, str)) and str(raw_value).strip():
                return str(raw_value).strip(), normalized_currency

        return None

    @classmethod
    def _candidate_from_state(cls, payload: dict[str, Any], source_url: str) -> PriceCandidate | None:
        product = payload.get("product")
        if not isinstance(product, dict):
            return None

        merchant_listing = product.get("merchantListing")
        if not isinstance(merchant_listing, dict):
            return None

        winner_variant = merchant_listing.get("winnerVariant")
        if not isinstance(winner_variant, dict):
            return None

        price_data = winner_variant.get("price")
        if not isinstance(price_data, dict):
            return None

        picked_price = cls._price_text(price_data)
        if picked_price is None:
            return None

        price_text, currency = picked_price
        parsed = scan_price_in_text(price_text, source="trendyol:winner-variant", require_currency=False)
        if not parsed:
            return None

        merchant = merchant_listing.get("merchant")
        seller = None
        if isinstance(merchant, dict):
            merchant_name = merchant.get("name")
            seller = str(merchant_name).strip() if isinstance(merchant_name, str) and merchant_name.strip() else None

        raw_listing_url = merchant_listing.get("url")
        listing_url = urljoin(source_url, raw_listing_url) if isinstance(raw_listing_url, str) else source_url

        return PriceCandidate(
            raw_text=parsed.raw_text,
            amount=parsed.amount,
            currency=parsed.currency or currency or "TRY",
            source=parsed.source,
            seller=seller,
            listing_url=listing_url,
            confidence="high",
            signals=_candidate_signals(
                "embedded_state",
                "winner_variant",
                seller=seller,
                listing_url=listing_url,
                currency=parsed.currency or currency or "TRY",
            ),
        )

    def _extract_from_envoy_state(self, page: Selector, url: str) -> PriceCandidate | None:
        html = str(getattr(page, "html_content", "") or "")
        if "__envoy_product-detail__PROPS" not in html:
            return None

        payload = _extract_json_object_after_marker(html, 'window["__envoy_product-detail__PROPS"]=')
        if not payload:
            return None

        return self._candidate_from_state(payload, url)

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        state_candidate = self._extract_from_envoy_state(page, url)
        if state_candidate is not None:
            return [state_candidate]

        selector_candidates = _extract_candidates_from_selectors(
            page,
            selectors=self._price_selectors,
            source_prefix=self.key,
            default_currency="TRY",
        )
        if selector_candidates:
            return selector_candidates

        return extract_price_listings_from_selector(page)


class AkakceAdapter(SiteAdapter):
    """Akakce-specific listing parser with seller cleanup rules."""

    key = "akakce"
    _row_selectors = ("#PL > li", "#PL li")
    _price_selectors = (
        ".pt_v8",
        "[class*='pt_v8']",
        "[class*='campaign']",
        "[class*='current']",
        "[class*='price']",
        "[data-price]",
    )

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
        seller_text = re.sub(r"^satıcı\s*:\s*", "", seller_text, flags=re.IGNORECASE).strip()
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

    @staticmethod
    def _select_price_node(price_nodes: list[Selector]) -> Selector:
        for price_node in price_nodes:
            class_name = str(getattr(price_node, "attrib", {}).get("class", ""))
            normalized_class = class_name.lower()
            if any(
                marker in normalized_class
                for marker in ("cmpgn_pt_v8", "campaign", "current", "discount", "sale")
            ):
                return price_node

        for price_node in price_nodes:
            class_name = str(getattr(price_node, "attrib", {}).get("class", ""))
            normalized_class = class_name.lower()
            if not any(marker in normalized_class for marker in ("orig_pt_v8", "original", "old", "was")):
                return price_node

        return price_nodes[0]

    def _listing_rows(self, page: Selector) -> list[Selector]:
        rows: list[Selector] = []
        for selector in self._row_selectors:
            for row in page.css(selector):
                if row not in rows:
                    rows.append(row)
        return rows

    def _price_nodes(self, row: Selector) -> list[Selector]:
        nodes: list[Selector] = []
        for selector in self._price_selectors:
            for node in row.css(selector):
                if node not in nodes:
                    nodes.append(node)
        return nodes

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        candidates: list[PriceCandidate] = []
        for li in self._listing_rows(page):
            price_nodes = self._price_nodes(li)
            if not price_nodes:
                continue

            price_node = self._select_price_node(list(price_nodes))
            price_text = self._normalize_price_text(str(price_node.get_all_text(separator=" ", strip=True)))
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
                    confidence="high",
                    signals=_candidate_signals(
                        "site_listing_row",
                        seller=seller_text,
                        listing_url=listing_url,
                        currency=candidate.currency or "TRY",
                    ),
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
                        confidence="high",
                        signals=_candidate_signals(
                            "site_listing_row",
                            seller=seller,
                            listing_url=str(listing_url).strip() if listing_url else None,
                            currency=candidate.currency,
                        ),
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
                        confidence="high",
                        signals=_candidate_signals(
                            "site_product_scope",
                            seller=str(seller_text).strip() if seller_text else None,
                            listing_url=url,
                            currency=candidate.currency,
                        ),
                    )
                ]

        return extract_price_listings_from_selector(page)


class N11Adapter(SiteAdapter):
    """n11 parser favoring selected seller price over generic aggregate offers."""

    key = "n11"

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

        payload = _extract_json_object_after_marker(html, "__INITIAL_STATE__")
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
                    confidence="high",
                    signals=_candidate_signals(
                        "embedded_state",
                        "site_listing_row",
                        seller=str(seller_name).strip() if isinstance(seller_name, str) else None,
                        listing_url=str(listing_url).strip() if isinstance(listing_url, str) else None,
                        currency=parsed.currency or "TRY",
                    ),
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

        product_meta = _extract_json_object_after_marker(html, '"productMeta":')
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
            confidence="high",
            signals=_candidate_signals(
                "embedded_state",
                "selected_offer",
                seller=str(seller_text).strip() if seller_text else None,
                listing_url=url,
                currency=parsed.currency or "TRY",
            ),
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
                    confidence="high",
                    signals=_candidate_signals(
                        "site_listing_row",
                        seller=str(seller).strip() if seller else None,
                        listing_url=str(listing_url).strip() if listing_url else None,
                        currency=parsed.currency or "TRY",
                    ),
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
