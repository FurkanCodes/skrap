from __future__ import annotations

"""Pure parsing/normalization helpers reused by extractors and adapters."""

import hashlib
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .constants import CURRENCY_TOKEN_TO_CODE, PRICE_PATTERN
from .domain import PriceCandidate

_TRACKING_QUERY_PARAM_PATTERN = re.compile(
    r"^(utm_|fbclid$|gclid$|yclid$|ref$|tag$|aff$|affiliate$|mc_cid$|mc_eid$)",
    re.IGNORECASE,
)
_ALIEXPRESS_ITEM_PATH_PATTERN = re.compile(r"/item/\d+\.html", re.IGNORECASE)
_ALIEXPRESS_PRODUCT_ID_PATTERN = re.compile(r"\d{10,}")
_AKAKCE_STABLE_LISTING_PARAMS = ("p", "v", "c", "g")


def normalize_currency(token: str | None) -> str | None:
    """Normalize symbol/code variants to canonical ISO-like codes."""
    if not token:
        return None
    normalized = token.strip().upper()
    return CURRENCY_TOKEN_TO_CODE.get(normalized)


def parse_decimal(raw_amount: str) -> Decimal | None:
    """Parse localized amount strings (thousands separators + decimal marks)."""
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


def scan_price_in_text(text: str, source: str, require_currency: bool) -> PriceCandidate | None:
    """Return the first price candidate found in a text fragment."""
    compact = " ".join(text.split())

    for match in PRICE_PATTERN.finditer(compact):
        currency_token = match.group("currency_before") or match.group("currency_after")
        if require_currency and not currency_token:
            continue

        amount = parse_decimal(match.group("amount"))
        if amount is None:
            continue

        raw = match.group(0).strip()
        return PriceCandidate(
            raw_text=raw,
            amount=amount,
            currency=normalize_currency(currency_token),
            source=source,
        )

    return None


def scan_prices_in_text(text: str, source: str, require_currency: bool) -> list[PriceCandidate]:
    """Return all price candidates found in a text fragment."""
    compact = " ".join(text.split())
    candidates: list[PriceCandidate] = []

    for match in PRICE_PATTERN.finditer(compact):
        currency_token = match.group("currency_before") or match.group("currency_after")
        if require_currency and not currency_token:
            continue

        amount = parse_decimal(match.group("amount"))
        if amount is None:
            continue

        raw = match.group(0).strip()
        candidates.append(
            PriceCandidate(
                raw_text=raw,
                amount=amount,
                currency=normalize_currency(currency_token),
                source=source,
            )
        )

    return candidates


def normalize_url(url: str, *, keep_fragment: bool = False) -> str:
    """Normalize URL for stable matching across scraping and tracking flows."""
    parsed = urlparse(url)
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _TRACKING_QUERY_PARAM_PATTERN.match(key)
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
            parsed.fragment if keep_fragment else "",
        )
    )


def normalize_akakce_listing_url(base_url: str, listing_url: str | None) -> str | None:
    """Return a stable Akakce offer URL, ignoring volatile redirect fragments."""
    raw_listing_url = (listing_url or "").strip()
    if not raw_listing_url:
        return None

    base_without_fragment = normalize_url(base_url, keep_fragment=False)
    absolute_url = (
        f"{base_without_fragment}{raw_listing_url}"
        if raw_listing_url.startswith("#")
        else urljoin(base_url, raw_listing_url)
    )
    parsed = urlparse(absolute_url)
    if not _is_akakce_host(parsed.netloc):
        return None

    fragment_query = parsed.fragment.partition("?")[2]
    if not fragment_query:
        return None

    params = parse_qs(fragment_query, keep_blank_values=True)
    stable_params = _akakce_stable_listing_params(params)
    if stable_params is None:
        return None

    stable_query = urlencode(stable_params)
    return f"{normalize_url(absolute_url, keep_fragment=False)}#/offer?{stable_query}"


def normalize_runtime_url(url: str) -> str:
    """Normalize runtime fetch URL for known non-product deep links.

    Some share links (notably AliExpress BundleDeals pages) include a
    `productIds` query value instead of a direct product URL. For scraping,
    rewrite to a canonical product-detail path so dedicated adapters can
    parse stable title/image/price fields.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if not host.endswith("aliexpress.com"):
        return url

    if _ALIEXPRESS_ITEM_PATH_PATTERN.search(parsed.path):
        return url

    query = parse_qs(parsed.query, keep_blank_values=True)
    raw_product_ids = query.get("productIds") or query.get("productId") or []
    product_id: str | None = None
    for raw_value in raw_product_ids:
        match = _ALIEXPRESS_PRODUCT_ID_PATTERN.search(raw_value)
        if match:
            product_id = match.group(0)
            break

    if not product_id:
        return url

    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "www.aliexpress.com"
    return urlunparse((scheme, netloc, f"/item/{product_id}.html", "", "", ""))


def strip_tracking_query_params(url: str) -> str:
    """Normalize URL and remove tracking parameters for stable ID generation."""
    return normalize_url(url, keep_fragment=False)


def make_product_id(url: str) -> str:
    """Generate stable product ID from normalized canonical URL."""
    normalized_url = strip_tracking_query_params(url)
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:20]
    return f"prd_{digest}"


def make_item_id(base_url: str, candidate: PriceCandidate, index: int) -> str:
    """Generate stable listing/item ID using listing URL when available.

    `index` is only used in fallback seeds where no listing URL exists.
    """
    normalized_base = strip_tracking_query_params(base_url)
    if candidate.listing_url:
        listing_ref = candidate.listing_url.strip()
        akakce_listing_ref = normalize_akakce_listing_url(base_url, listing_ref)
        if akakce_listing_ref is not None:
            seed = f"akakce-offer|{akakce_listing_ref}"
        elif listing_ref.startswith("#"):
            seed = f"url-fragment|{normalized_base}|{listing_ref}"
        else:
            absolute_listing = urljoin(base_url, listing_ref)
            normalized_listing = strip_tracking_query_params(absolute_listing)
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


def _is_akakce_host(netloc: str) -> bool:
    host = netloc.lower().split("@")[-1].split(":")[0].removeprefix("www.")
    return host == "akakce.com" or host.endswith(".akakce.com")


def _akakce_stable_listing_params(
    params: dict[str, list[str]],
) -> list[tuple[str, str]] | None:
    has_offer_identity = bool(params.get("g")) or bool(params.get("p") and params.get("v"))
    if not has_offer_identity:
        return None

    stable_params: list[tuple[str, str]] = []
    for key in _AKAKCE_STABLE_LISTING_PARAMS:
        values = params.get(key)
        if values and values[0]:
            stable_params.append((key, values[0]))
    if len(stable_params) < 2:
        return None
    return stable_params
