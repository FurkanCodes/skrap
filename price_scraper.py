from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from scrapling.fetchers import Fetcher
from scrapling.parser import Selector


CURRENCY_TOKEN_TO_CODE = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "¥": "JPY",
    "JPY": "JPY",
    "₺": "TRY",
    "TL": "TRY",
    "TRY": "TRY",
    "₹": "INR",
    "INR": "INR",
    "₽": "RUB",
    "RUB": "RUB",
    "₩": "KRW",
    "KRW": "KRW",
    "AED": "AED",
    "SAR": "SAR",
    "QAR": "QAR",
    "KWD": "KWD",
    "BHD": "BHD",
    "OMR": "OMR",
    "CAD": "CAD",
    "AUD": "AUD",
    "NZD": "NZD",
    "CHF": "CHF",
    "CNY": "CNY",
    "HKD": "HKD",
    "SGD": "SGD",
    "BRL": "BRL",
    "MXN": "MXN",
    "ZAR": "ZAR",
    "PLN": "PLN",
    "NOK": "NOK",
    "SEK": "SEK",
    "DKK": "DKK",
}


CURRENCY_TOKEN_PATTERN = "|".join(
    sorted((re.escape(token) for token in CURRENCY_TOKEN_TO_CODE), key=len, reverse=True)
)

PRICE_PATTERN = re.compile(
    rf"(?P<currency_before>{CURRENCY_TOKEN_PATTERN})?\s*"
    r"(?P<amount>(?:\d{1,3}(?:[\s.,]\d{3})+|\d+)(?:[.,]\d{2})?)"
    rf"\s*(?P<currency_after>{CURRENCY_TOKEN_PATTERN})?",
    re.IGNORECASE,
)


META_PRICE_SELECTORS = [
    ("meta[itemprop='price']::attr(content)", "meta[itemprop=price]"),
    ("meta[property='product:price:amount']::attr(content)", "meta[property=product:price:amount]"),
    ("meta[property='og:price:amount']::attr(content)", "meta[property=og:price:amount]"),
    ("meta[property='twitter:data1']::attr(content)", "meta[property=twitter:data1]"),
]

META_CURRENCY_SELECTORS = [
    "meta[itemprop='priceCurrency']::attr(content)",
    "meta[property='product:price:currency']::attr(content)",
    "meta[property='og:price:currency']::attr(content)",
]

DOM_PRICE_SELECTORS = [
    "[itemprop='price']",
    "[data-price]",
    "[data-testid*='price']",
    ".a-price .a-offscreen",
    ".price-current",
    ".product-price",
    ".sale-price",
    ".our-price",
    ".price",
    "[class*='price']",
    "[id*='price']",
]

SUPPORTED_SITES: dict[str, str] = {
    "generic": "Generic extractor (fallback for any site)",
    "akakce": "Akakce adapter",
    "newegg": "Newegg adapter",
}

SITE_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "akakce": ("akakce.com",),
    "newegg": ("newegg.com",),
}


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


def parse_urls_blob(blob: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://\S+", blob):
        token = match.group(0).strip()
        # Support comma/semicolon-separated URLs in one token while preserving commas inside URL paths.
        for candidate in re.split(r"[;,](?=https?://)", token):
            cleaned = candidate.rstrip('"\')]}>,;:,')
            if cleaned.startswith(("http://", "https://")):
                urls.append(cleaned)
    # Keep order but drop duplicates.
    return list(dict.fromkeys(urls))


def parse_sites_blob(blob: str) -> set[str]:
    tokens = [token.strip().lower() for token in re.split(r"[\s,;]+", blob) if token.strip()]
    if not tokens:
        return set()

    selected: set[str] = set()
    for token in tokens:
        if token in {"all", "*"}:
            return set()
        if token not in SUPPORTED_SITES:
            supported = ", ".join(sorted(SUPPORTED_SITES))
            raise ValueError(f"Unsupported site key '{token}'. Supported keys: {supported}")
        selected.add(token)
    return selected


def detect_site_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    for site_key, suffixes in SITE_DOMAIN_SUFFIXES.items():
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
            return site_key
    return "generic"


def _normalize_currency(token: str | None) -> str | None:
    if not token:
        return None
    normalized = token.strip().upper()
    return CURRENCY_TOKEN_TO_CODE.get(normalized)


def _parse_decimal(raw_amount: str) -> Decimal | None:
    cleaned = raw_amount.replace(" ", "")

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) > 1 and len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = cleaned.replace(".", "")
        elif len(parts) == 2 and len(parts[-1]) == 3:
            cleaned = cleaned.replace(".", "")

    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _scan_price_in_text(text: str, source: str, require_currency: bool) -> PriceCandidate | None:
    compact = " ".join(text.split())

    for match in PRICE_PATTERN.finditer(compact):
        currency_token = match.group("currency_before") or match.group("currency_after")
        if require_currency and not currency_token:
            continue

        amount = _parse_decimal(match.group("amount"))
        if amount is None:
            continue

        raw = match.group(0).strip()
        return PriceCandidate(
            raw_text=raw,
            amount=amount,
            currency=_normalize_currency(currency_token),
            source=source,
        )

    return None


def _scan_prices_in_text(text: str, source: str, require_currency: bool) -> list[PriceCandidate]:
    compact = " ".join(text.split())
    candidates: list[PriceCandidate] = []

    for match in PRICE_PATTERN.finditer(compact):
        currency_token = match.group("currency_before") or match.group("currency_after")
        if require_currency and not currency_token:
            continue

        amount = _parse_decimal(match.group("amount"))
        if amount is None:
            continue

        raw = match.group(0).strip()
        candidates.append(
            PriceCandidate(
                raw_text=raw,
                amount=amount,
                currency=_normalize_currency(currency_token),
                source=source,
            )
        )

    return candidates


def _strip_tracking_query_params(url: str) -> str:
    parsed = urlparse(url)
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not re.match(
            r"^(utm_|fbclid$|gclid$|yclid$|ref$|tag$|aff$|affiliate$|mc_cid$|mc_eid$)",
            key,
            re.IGNORECASE,
        )
    ]
    normalized_query = urlencode(sorted(filtered_query), doseq=True)
    normalized_path = parsed.path or "/"
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            normalized_path,
            "",
            normalized_query,
            "",
        )
    )


def _make_product_id(url: str) -> str:
    normalized_url = _strip_tracking_query_params(url)
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:20]
    return f"prd_{digest}"


def _make_item_id(base_url: str, candidate: PriceCandidate, index: int) -> str:
    normalized_base = _strip_tracking_query_params(base_url)
    if candidate.listing_url:
        listing_ref = candidate.listing_url.strip()
        if listing_ref.startswith("#"):
            # Hash-route URLs are common on comparison sites (e.g. Akakce) and are
            # still unique listing references; keep them in the seed.
            seed = f"url-fragment|{normalized_base}|{listing_ref}"
        else:
            absolute_listing = urljoin(base_url, listing_ref)
            normalized_listing = _strip_tracking_query_params(absolute_listing)
            seed = f"url|{normalized_listing}"
    else:
        seed = "|".join(
            [
                "fallback",
                normalized_base,
                (candidate.seller or "").strip().lower(),
                (candidate.currency or "").strip().upper(),
                format(candidate.amount, "f"),
                candidate.source.strip().lower(),
                candidate.raw_text.strip().lower(),
                str(index),
            ]
        )

    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"itm_{digest}"


def _iter_json_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_nodes(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_nodes(item)


def _candidate_from_price_data(data: dict[str, Any], source: str) -> PriceCandidate | None:
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
        amount = _parse_decimal(str(direct_price))
        if amount is not None:
            return PriceCandidate(
                raw_text=str(direct_price),
                amount=amount,
                currency=_normalize_currency(str(direct_currency)) if direct_currency else None,
                source=source,
                seller=seller_name,
                listing_url=listing_url,
            )

    for key in ("lowPrice", "highPrice"):
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            amount = _parse_decimal(str(value))
            if amount is not None:
                return PriceCandidate(
                    raw_text=str(value),
                    amount=amount,
                    currency=_normalize_currency(str(direct_currency)) if direct_currency else None,
                    source=source,
                    seller=seller_name,
                    listing_url=listing_url,
                )

    return None


def _candidates_from_dict(data: dict[str, Any], source: str) -> list[PriceCandidate]:
    candidates: list[PriceCandidate] = []
    offers = data.get("offers")

    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                candidates.extend(_candidates_from_dict(offer, source))
    elif isinstance(offers, dict):
        candidates.extend(_candidates_from_dict(offers, source))

    # If we got explicit offers, they are the actual listings we want.
    if candidates:
        return candidates

    direct = _candidate_from_price_data(data, source)
    return [direct] if direct else []


def _deduplicate_candidates(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
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


def _extract_generic_price_listings_from_selector(page: Selector) -> list[PriceCandidate]:
    meta_currency: str | None = None
    for selector in META_CURRENCY_SELECTORS:
        value = page.css(selector).get()
        if value:
            meta_currency = _normalize_currency(str(value))
            if meta_currency:
                break

    # 1) Structured data first: this is where multi-listing pages usually expose all offers.
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
        return _deduplicate_candidates(json_candidates)

    # 2) DOM selectors: capture one candidate per matching element.
    dom_candidates: list[PriceCandidate] = []
    for selector in DOM_PRICE_SELECTORS:
        for element in page.css(selector)[:300]:
            text_candidates = [
                str(element.get_all_text(separator=" ", strip=True)),
                str(element.attrib.get("content", "")),
                str(element.attrib.get("data-price", "")),
                str(element.attrib.get("value", "")),
                str(element.attrib.get("aria-label", "")),
            ]

            listing_url = None
            href_value = element.attrib.get("href", "")
            if href_value:
                listing_url = str(href_value)

            for text in text_candidates:
                if not text:
                    continue
                candidate = _scan_price_in_text(text, source=f"dom:{selector}", require_currency=False)
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
        return _deduplicate_candidates(dom_candidates)

    # 3) Metadata fallback.
    for selector, source in META_PRICE_SELECTORS:
        value = page.css(selector).get()
        if not value:
            continue
        candidate = _scan_price_in_text(str(value), source=source, require_currency=False)
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

    # 4) Fallback to full-page text, requiring currency token to reduce false positives.
    all_text = str(page.get_all_text(separator=" ", strip=True))
    fallback_candidates = _scan_prices_in_text(all_text, source="page-text", require_currency=True)[:200]
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
    return _deduplicate_candidates(normalized_fallback)


class SiteAdapter:
    key = "generic"

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        raise NotImplementedError


class GenericAdapter(SiteAdapter):
    key = "generic"

    def extract(self, page: Selector, url: str) -> list[PriceCandidate]:
        return _extract_generic_price_listings_from_selector(page)


class AkakceAdapter(SiteAdapter):
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
            candidate = _scan_price_in_text(price_text, source="akakce:dom", require_currency=False)
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
            return _deduplicate_candidates(candidates)

        return _extract_generic_price_listings_from_selector(page)


class NeweggAdapter(SiteAdapter):
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
                candidate = _scan_price_in_text(price_text, source="newegg:listing", require_currency=False)
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
                return _deduplicate_candidates(candidates)

        # Product detail fallback
        price_nodes = page.css(".price-current")
        if price_nodes:
            price_text = str(price_nodes[0].get_all_text(separator=" ", strip=True))
            candidate = _scan_price_in_text(price_text, source="newegg:product", require_currency=False)
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

        return _extract_generic_price_listings_from_selector(page)


ADAPTERS: dict[str, SiteAdapter] = {
    "generic": GenericAdapter(),
    "akakce": AkakceAdapter(),
    "newegg": NeweggAdapter(),
}


def extract_price_listings_from_selector(page: Selector) -> list[PriceCandidate]:
    return _extract_generic_price_listings_from_selector(page)


def extract_price_from_selector(page: Selector) -> PriceCandidate | None:
    listings = _extract_generic_price_listings_from_selector(page)
    return listings[0] if listings else None


def fetch_price(url: str, timeout: int = 30, selected_sites: set[str] | None = None) -> PriceResult:
    product_id = _make_product_id(url)
    site_key = detect_site_key(url)
    adapter_key = site_key if site_key in ADAPTERS else "generic"

    if selected_sites is not None and adapter_key not in selected_sites:
        return PriceResult(
            url=url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=f"site_filtered_out:{adapter_key}",
        )

    try:
        page = Fetcher.get(
            url,
            timeout=timeout,
            retries=2,
            retry_delay=1,
            stealthy_headers=True,
            follow_redirects="safe",
        )
    except Exception as exc:
        return PriceResult(
            url=url,
            success=False,
            product_id=product_id,
            site_key=site_key,
            adapter_key=adapter_key,
            error=f"fetch_failed: {exc}",
        )

    adapter = ADAPTERS[adapter_key]
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
            product_id=product_id,
            site_key=site_key,
            adapter_key=used_adapter,
            error="price_not_found",
        )

    listings = [
        PriceListing(
            item_id=_make_item_id(url, candidate, index),
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


def scrape_prices(urls: Iterable[str], timeout: int = 30, selected_sites: set[str] | None = None) -> list[PriceResult]:
    return [fetch_price(url, timeout=timeout, selected_sites=selected_sites) for url in urls]


def _print_table(results: list[PriceResult]) -> None:
    if not results:
        print("No URLs were provided.")
        return

    headers = [
        "URL",
        "Product ID",
        "Listing #",
        "Item ID",
        "Amount",
        "Currency",
        "Seller",
        "Listing URL",
        "Source",
        "Status",
    ]
    rows: list[list[str]] = []
    for result in results:
        if result.success and result.listings:
            for index, listing in enumerate(result.listings, 1):
                rows.append(
                    [
                        result.url if index == 1 else "",
                        (result.product_id or "-") if index == 1 else "",
                        str(index),
                        listing.item_id,
                        listing.amount,
                        listing.currency or "-",
                        listing.seller or "-",
                        listing.listing_url or "-",
                        listing.source,
                        "ok",
                    ]
                )
            continue

        status = result.error or "failed"
        rows.append([result.url, result.product_id or "-", "-", "-", "-", "-", "-", "-", "-", status])

    widths = [len(column) for column in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def format_row(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    print(format_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row))


def _collect_input_urls(args_urls: list[str]) -> list[str]:
    if args_urls:
        return parse_urls_blob("\n".join(args_urls))

    if sys.stdin.isatty():
        print("Paste product URLs (one per line), then press Ctrl+D:", file=sys.stderr)

    return parse_urls_blob(sys.stdin.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch product prices from URLs.")
    parser.add_argument("urls", nargs="*", help="One or more product URLs")
    parser.add_argument("--timeout", type=int, default=30, help="Request timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument(
        "--sites",
        type=str,
        default="",
        help="Comma-separated site keys to include (e.g. akakce,newegg,generic). Default is all.",
    )
    parser.add_argument("--list-sites", action="store_true", help="List supported site adapters and exit")
    args = parser.parse_args()

    if args.list_sites:
        for key in sorted(SUPPORTED_SITES):
            print(f"{key}: {SUPPORTED_SITES[key]}")
        return 0

    try:
        selected_sites = parse_sites_blob(args.sites)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    selected_sites_or_none = selected_sites if selected_sites else None
    urls = _collect_input_urls(args.urls)
    results = scrape_prices(urls, timeout=args.timeout, selected_sites=selected_sites_or_none)

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        _print_table(results)

    return 0 if all(result.success for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
