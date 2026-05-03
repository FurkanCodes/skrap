from __future__ import annotations

"""Product metadata extraction helpers (JSON-LD -> meta tags -> DOM)."""

import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from scrapling.parser import Selector

from .availability import is_canonical_availability, normalize_availability as _normalize_availability

_COMMON_AVAILABILITY_SELECTORS = (
    "link[itemprop='availability']::attr(href)",
    "[itemprop='availability']::attr(href)",
    "[data-availability]::attr(data-availability)",
    "[data-stock]::attr(data-stock)",
    "[data-stock-status]::attr(data-stock-status)",
    "[data-stock-status]::text",
    "[data-testid*='availability']::text",
    "[data-test-id*='availability']::text",
    "[data-test*='availability']::text",
    "[id*='availability']::text",
    "[class*='availability']::text",
    "[id*='stock']::text",
    "[class*='stock']::text",
    "[aria-label*='stock']::attr(aria-label)",
    "[aria-label*='stok']::attr(aria-label)",
    ".availability::text",
)

_SITE_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "a101": ("a101.com.tr",),
    "aliexpress": ("aliexpress.com",),
    "amazon": ("amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.es", "amazon.com.tr"),
    "bestbuy": ("bestbuy.com",),
    "ciceksepeti": ("ciceksepeti.com",),
    "costco": ("costco.com",),
    "ebay": ("ebay.com", "ebay.co.uk", "ebay.de", "ebay.fr", "ebay.es"),
    "etsy": ("etsy.com",),
    "hepsiburada": ("hepsiburada.com",),
    "homedepot": ("homedepot.com",),
    "ikea": ("ikea.com",),
    "leroymerlin": ("leroymerlin.fr", "leroymerlin.es", "leroymerlin.it"),
    "lowes": ("lowes.com",),
    "mediamarkt": ("mediamarkt.com", "mediamarkt.de", "mediamarkt.es", "mediamarkt.com.tr"),
    "mediaworld": ("mediaworld.it",),
    "n11": ("n11.com",),
    "pttavm": ("pttavm.com",),
    "shein": ("shein.com",),
    "target": ("target.com",),
    "temu": ("temu.com",),
    "teknosa": ("teknosa.com",),
    "trendyol": ("trendyol.com",),
    "vatanbilgisayar": ("vatanbilgisayar.com",),
    "vinted": ("vinted.fr", "vinted.it", "vinted.es"),
    "zalando": ("zalando.de", "zalando.it", "zalando.es"),
}

_SITE_TITLE_SELECTORS: dict[str, tuple[str, ...]] = {
    "a101": ("h1::text",),
    "aliexpress": ("h1[data-pl='product-title']::text", "h1::text"),
    "amazon": ("#productTitle::text", "h1#title::text", "h1::text"),
    "bestbuy": ("h1[data-testid='product-title']::text", "h1::text"),
    "ciceksepeti": ("h1::text",),
    "costco": ("h1.product-name::text", "h1::text"),
    "ebay": ("h1.x-item-title__mainTitle span::text", "h1::text"),
    "etsy": ("h1[data-buy-box-listing-title='true']::text", "h1::text"),
    "hepsiburada": ("h1[data-test-id='title']::text", "h1::text"),
    "homedepot": ("h1[data-testid='product-label']::text", "h1::text"),
    "ikea": ("h1[data-testid='pip-product-title']::text", "h1::text"),
    "leroymerlin": ("h1::text",),
    "lowes": ("h1[data-testid='product-name']::text", "h1::text"),
    "mediamarkt": ("h1::text",),
    "mediaworld": ("h1::text",),
    "n11": ("h1::text",),
    "pttavm": ("h1.product-detail__name::text", "h1::text"),
    "shein": ("h1.product-intro__head-name::text", "h1::text"),
    "target": ("[data-test='product-title']::text", "h1::text"),
    "temu": ("h1[data-testid='product-name']::text", "h1::text"),
    "teknosa": ("h1::text",),
    "trendyol": (".pr-new-br span::text", "h1::text"),
    "vatanbilgisayar": ("h1::text",),
    "vinted": ("h1[data-testid='item-title']::text", "h1::text"),
    "zalando": ("h1[data-testid='product-name']::text", "h1::text"),
}

_SITE_IMAGE_SELECTORS: dict[str, tuple[str, ...]] = {
    "a101": (".pdp-image img::attr(src)", "img::attr(src)"),
    "aliexpress": (".magnifier-image::attr(src)", "img::attr(src)"),
    "amazon": ("#landingImage::attr(src)", "img::attr(src)"),
    "bestbuy": ("img.primary-image::attr(src)", "img::attr(src)"),
    "ciceksepeti": ("img::attr(src)",),
    "costco": ("img#lens-image::attr(src)", "img::attr(src)"),
    "ebay": ("img#icImg::attr(src)", "[data-zoom-src]::attr(data-zoom-src)", "img::attr(src)"),
    "etsy": ("[data-carousel-pane] img::attr(src)", "img::attr(src)"),
    "hepsiburada": ("img[data-test-id='image']::attr(src)", "img::attr(src)"),
    "homedepot": ("img[data-testid='hero-image']::attr(src)", "img::attr(src)"),
    "ikea": ("img[data-testid='pip-main-image']::attr(src)", "img::attr(src)"),
    "leroymerlin": ("[data-qa-product-image]::attr(src)", "img::attr(src)"),
    "lowes": ("img[data-testid='pdp-main-image']::attr(src)", "img::attr(src)"),
    "mediamarkt": ("img::attr(src)",),
    "mediaworld": ("img::attr(src)",),
    "n11": ("img::attr(src)",),
    "pttavm": (".product-detail-gallery img::attr(src)", "img::attr(src)"),
    "shein": (".product-intro__thumbs-inner img::attr(src)", "img::attr(src)"),
    "target": ("img[data-test='product-image']::attr(data-src)", "img[data-test='product-image']::attr(src)", "img::attr(src)"),
    "temu": ("img::attr(src)",),
    "teknosa": ("img::attr(src)",),
    "trendyol": ("img::attr(src)",),
    "vatanbilgisayar": (".swiper-slide img::attr(src)", "img::attr(src)"),
    "vinted": ("[data-testid='item-photo'] img::attr(src)", "img::attr(src)"),
    "zalando": ("img[data-testid='media-image']::attr(src)", "img::attr(src)"),
}


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text or None


def _iter_json_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_nodes(child)


def _pick_name(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        return _clean_text(value.get("name"))
    return None


def _pick_image(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        for item in value:
            picked = _pick_image(item)
            if picked:
                return picked
        return None
    if isinstance(value, dict):
        return _clean_text(value.get("url")) or _clean_text(value.get("contentUrl"))
    return None


def _extract_json_object_after_marker(text: str, marker: str) -> dict[str, Any] | None:
    marker_index = text.find(marker)
    if marker_index < 0:
        return None

    start = text.find("{", marker_index)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
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
                chunk = text[start : index + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return parsed
                return None
    return None


def _extract_from_embedded_state(page: Selector, source_url: str) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {
        "product_name": None,
        "brand": None,
        "model": None,
        "sku": None,
        "gtin": None,
        "image_url": None,
        "canonical_url": None,
        "availability": None,
    }

    html = str(getattr(page, "html_content", "") or "")
    if not html:
        return metadata

    json_ld_product = _extract_json_object_after_marker(html, '"jsonLDProduct":')
    catalog_product = _extract_json_object_after_marker(html, '"catalogProductInfo":')
    product_meta = _extract_json_object_after_marker(html, '"productMeta":')

    if isinstance(json_ld_product, dict):
        metadata["product_name"] = _pick_name(json_ld_product.get("name"))
        metadata["brand"] = _pick_name(json_ld_product.get("brand"))
        metadata["sku"] = _clean_text(json_ld_product.get("sku"))
        metadata["gtin"] = (
            _clean_text(json_ld_product.get("gtin"))
            or _clean_text(json_ld_product.get("gtin13"))
            or _clean_text(json_ld_product.get("gtin14"))
            or _clean_text(json_ld_product.get("gtin12"))
            or _clean_text(json_ld_product.get("gtin8"))
        )
        metadata["image_url"] = _pick_image(json_ld_product.get("image"))
        offers = json_ld_product.get("offers")
        if isinstance(offers, dict):
            metadata["availability"] = _normalize_availability(_clean_text(offers.get("availability")))
            metadata["canonical_url"] = _clean_text(offers.get("url"))

    if isinstance(catalog_product, dict):
        metadata["product_name"] = metadata["product_name"] or _clean_text(catalog_product.get("title"))
        metadata["image_url"] = metadata["image_url"] or _pick_image(catalog_product.get("imageUrl"))

    if isinstance(product_meta, dict):
        metadata["brand"] = metadata["brand"] or _clean_text(product_meta.get("brand"))
        metadata["sku"] = metadata["sku"] or _clean_text(product_meta.get("sku")) or _clean_text(product_meta.get("skuId"))

    if metadata["image_url"]:
        metadata["image_url"] = urljoin(source_url, metadata["image_url"])
    if metadata["canonical_url"]:
        metadata["canonical_url"] = urljoin(source_url, metadata["canonical_url"])

    return metadata


def _is_site_level_name(value: str | None, source_url: str) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    host = urlparse(source_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    normalized = text.lower().removeprefix("www.")
    return normalized == host


def _is_homepage_url(candidate: str | None, source_url: str) -> bool:
    if not candidate:
        return False
    parsed_candidate = urlparse(candidate)
    parsed_source = urlparse(source_url)
    host_candidate = parsed_candidate.netloc.lower().removeprefix("www.")
    host_source = parsed_source.netloc.lower().removeprefix("www.")
    if host_candidate != host_source:
        return False
    return (parsed_candidate.path or "/") == "/"


def _is_n11_host(source_url: str) -> bool:
    host = urlparse(source_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "n11.com" or host.endswith(".n11.com")


def _is_trendyol_host(source_url: str) -> bool:
    host = urlparse(source_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "trendyol.com" or host.endswith(".trendyol.com")


def _is_amazon_host(source_url: str) -> bool:
    host = urlparse(source_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "amazon.com.tr" or host.startswith("amazon.") or ".amazon." in host


def _extract_amazon_availability(page: Selector) -> str | None:
    selectors = (
        "#availability span::text",
        "#availabilityInsideBuyBox_feature_div #availability span::text",
        "#availabilityOutsideBuyBox_feature_div #availability span::text",
        "#outOfStock span::text",
        "#outOfStock::text",
        "#outOfStock_feature_div span::text",
    )
    for selector in selectors:
        for value in page.css(selector).getall():
            cleaned = _clean_text(value)
            if cleaned:
                return cleaned
    return None


def _extract_common_availability_hint(page: Selector) -> str | None:
    for selector in _COMMON_AVAILABILITY_SELECTORS:
        for value in page.css(selector).getall():
            cleaned = _clean_text(value)
            if not cleaned:
                continue
            normalized = _normalize_availability(cleaned)
            if is_canonical_availability(normalized):
                return cleaned
    return None


def _detect_site_key_from_url(source_url: str) -> str | None:
    host = urlparse(source_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for site_key, suffixes in _SITE_DOMAIN_SUFFIXES.items():
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
            return site_key
    return None


def _is_trendyol_non_product_image(url: str) -> bool:
    normalized = url.lower()
    if normalized.endswith(".svg"):
        return True
    noisy_tokens = (
        "/sfint/prod/fp/",
        "/web/prod/fp/",
        "/mweb/production/",
        "apple-icon",
        "splash",
        "logo",
        "search_",
        "favorite_",
        "basket-icon_",
    )
    return any(token in normalized for token in noisy_tokens)


def _extract_trendyol_image(page: Selector) -> str | None:
    html = str(getattr(page, "html_content", "") or "")
    if not html:
        return None

    candidates = re.findall(
        r"https://cdn\.dsmcdn\.com/[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp|avif)",
        html,
        flags=re.IGNORECASE,
    )
    if not candidates:
        return None

    scored: list[tuple[int, str]] = []
    for candidate in candidates:
        normalized = candidate.lower()
        if _is_trendyol_non_product_image(normalized):
            continue

        score = 0
        if "/mnresize/" in normalized:
            score += 3
        if "/prod/" in normalized:
            score += 2
        if "org_zoom" in normalized:
            score += 1
        scored.append((score, candidate))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _gallery_image_rank(url: str) -> int:
    normalized = url.lower()
    if "/375_535/" in normalized:
        return 0
    if "/640/" in normalized:
        return 1
    if "/60_86/" in normalized:
        return 4
    return 2


def _extract_gallery_image(page: Selector, source_url: str) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    ordered_selectors = [
        ".img-wrapper-onBoard img.active::attr(src)",
        ".img-wrapper-onBoard img::attr(src)",
        ".desktop-slider img.active::attr(src)",
        ".desktop-slider img::attr(src)",
    ]
    for selector_priority, selector in enumerate(ordered_selectors):
        for raw in page.css(selector).getall():
            cleaned = _clean_text(raw)
            if cleaned:
                resolved = urljoin(source_url, cleaned)
                candidates.append((selector_priority, _gallery_image_rank(resolved), resolved))
                # n11 often exposes selected image as a thumbnail path. Promote a
                # predictable high-res variant for metadata consumers.
                if "/60_86/" in resolved:
                    candidates.append((selector_priority, 0, resolved.replace("/60_86/", "/375_535/")))

    html = str(getattr(page, "html_content", "") or "")
    if html:
        for match in re.finditer(r"https://n11scdn[^\"'\\s>]+", html, re.IGNORECASE):
            candidate = _clean_text(match.group(0))
            if candidate and ("/375_535/" in candidate.lower() or "/640/" in candidate.lower()):
                candidates.append((10, _gallery_image_rank(candidate), candidate))

    if not candidates:
        return None

    best_by_url: dict[str, tuple[int, int, str]] = {}
    for selector_priority, rank, candidate in candidates:
        current = best_by_url.get(candidate)
        next_value = (selector_priority, rank, candidate)
        if current is None or next_value < current:
            best_by_url[candidate] = next_value

    best = min(best_by_url.values(), default=None)
    return best[2] if best else None


def _extract_from_json_ld(page: Selector, source_url: str) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {
        "product_name": None,
        "brand": None,
        "model": None,
        "sku": None,
        "gtin": None,
        "image_url": None,
        "canonical_url": None,
        "availability": None,
    }

    product_node: dict[str, Any] | None = None
    first_node: dict[str, Any] | None = None
    for script_text in page.css("script[type='application/ld+json']::text").getall():
        text = _clean_text(script_text)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        for node in _iter_json_nodes(payload):
            if first_node is None:
                first_node = node
            node_type = node.get("@type")
            node_types: list[str] = []
            if isinstance(node_type, str):
                node_types = [node_type.lower()]
            elif isinstance(node_type, list):
                node_types = [str(item).lower() for item in node_type]
            if any("product" in item for item in node_types):
                product_node = node
                break
        if product_node is not None:
            break

    node = product_node or first_node
    if node is None:
        return metadata

    metadata["product_name"] = _pick_name(node.get("name")) or _pick_name(node.get("headline"))
    metadata["brand"] = _pick_name(node.get("brand")) or _pick_name(node.get("manufacturer"))
    metadata["model"] = _clean_text(node.get("model")) or _clean_text(node.get("mpn"))
    metadata["sku"] = _clean_text(node.get("sku"))
    metadata["gtin"] = (
        _clean_text(node.get("gtin"))
        or _clean_text(node.get("gtin13"))
        or _clean_text(node.get("gtin14"))
        or _clean_text(node.get("gtin12"))
    )
    metadata["image_url"] = _pick_image(node.get("image"))
    metadata["canonical_url"] = _clean_text(node.get("url"))

    offers = node.get("offers")
    offer_node: dict[str, Any] | None = None
    if isinstance(offers, dict):
        offer_node = offers
    elif isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                offer_node = offer
                break
    if isinstance(offer_node, dict):
        metadata["availability"] = _normalize_availability(_clean_text(offer_node.get("availability")))
        metadata["canonical_url"] = metadata["canonical_url"] or _clean_text(offer_node.get("url"))

    if metadata["canonical_url"]:
        metadata["canonical_url"] = urljoin(source_url, metadata["canonical_url"])
    if metadata["image_url"]:
        metadata["image_url"] = urljoin(source_url, metadata["image_url"])
    return metadata


def _first_css(page: Selector, selectors: list[str]) -> str | None:
    for selector in selectors:
        value = page.css(selector).get()
        text = _clean_text(value)
        if text:
            return text
    return None


def _apply_site_specific_metadata_fallback(
    page: Selector,
    source_url: str,
    metadata: dict[str, str | None],
) -> None:
    site_key = _detect_site_key_from_url(source_url)
    if not site_key:
        return

    if metadata["product_name"] is None:
        title_selectors = list(_SITE_TITLE_SELECTORS.get(site_key, ()))
        if title_selectors:
            metadata["product_name"] = _first_css(page, title_selectors)

    if metadata["image_url"] is None:
        image_selectors = list(_SITE_IMAGE_SELECTORS.get(site_key, ()))
        if image_selectors:
            image_candidate = _first_css(page, image_selectors)
            if image_candidate:
                metadata["image_url"] = urljoin(source_url, image_candidate)


def extract_product_metadata(page: Selector, source_url: str) -> dict[str, str | None]:
    """Extract normalized product metadata from a parsed page."""
    metadata = _extract_from_json_ld(page, source_url)
    embedded = _extract_from_embedded_state(page, source_url)

    site_level_name = _is_site_level_name(metadata["product_name"], source_url)
    if embedded["product_name"] and (metadata["product_name"] is None or site_level_name):
        metadata["product_name"] = embedded["product_name"]
    if embedded["image_url"] and (metadata["image_url"] is None or site_level_name):
        metadata["image_url"] = embedded["image_url"]
    metadata["brand"] = metadata["brand"] or embedded["brand"]
    metadata["model"] = metadata["model"] or embedded["model"]
    metadata["sku"] = metadata["sku"] or embedded["sku"]
    metadata["gtin"] = metadata["gtin"] or embedded["gtin"]
    metadata["availability"] = metadata["availability"] or embedded["availability"]
    if metadata["canonical_url"] is None:
        metadata["canonical_url"] = embedded["canonical_url"]

    metadata["product_name"] = metadata["product_name"] or _first_css(
        page,
        [
            "meta[property='og:title']::attr(content)",
            "meta[name='twitter:title']::attr(content)",
            "meta[itemprop='name']::attr(content)",
            "h1::text",
            "title::text",
        ],
    )
    _apply_site_specific_metadata_fallback(page, source_url, metadata)
    metadata["brand"] = metadata["brand"] or _first_css(
        page,
        [
            "meta[name='brand']::attr(content)",
            "meta[itemprop='brand']::attr(content)",
        ],
    )
    metadata["model"] = metadata["model"] or _first_css(
        page,
        [
            "meta[itemprop='model']::attr(content)",
            "meta[name='model']::attr(content)",
        ],
    )
    metadata["sku"] = metadata["sku"] or _first_css(
        page,
        [
            "meta[itemprop='sku']::attr(content)",
            "meta[name='sku']::attr(content)",
        ],
    )
    metadata["gtin"] = metadata["gtin"] or _first_css(
        page,
        [
            "meta[itemprop='gtin13']::attr(content)",
            "meta[itemprop='gtin14']::attr(content)",
            "meta[itemprop='gtin12']::attr(content)",
        ],
    )

    image_url = metadata["image_url"] or _first_css(
        page,
        [
            "meta[property='og:image']::attr(content)",
            "meta[name='twitter:image']::attr(content)",
            "img::attr(src)",
        ],
    )
    metadata["image_url"] = urljoin(source_url, image_url) if image_url else None
    if _is_trendyol_host(source_url) and (
        metadata["image_url"] is None or _is_trendyol_non_product_image(metadata["image_url"])
    ):
        trendyol_image = _extract_trendyol_image(page)
        if trendyol_image:
            metadata["image_url"] = trendyol_image
    if _is_n11_host(source_url):
        gallery_image = _extract_gallery_image(page, source_url)
        if gallery_image:
            metadata["image_url"] = gallery_image

    canonical_url = metadata["canonical_url"] or _first_css(
        page,
        [
            "link[rel='canonical']::attr(href)",
            "meta[property='og:url']::attr(content)",
        ],
    )
    metadata["canonical_url"] = urljoin(source_url, canonical_url) if canonical_url else source_url
    if _is_homepage_url(metadata["canonical_url"], source_url):
        metadata["canonical_url"] = source_url

    if not metadata["availability"]:
        if _is_amazon_host(source_url):
            metadata["availability"] = _normalize_availability(_extract_amazon_availability(page))

    if not metadata["availability"]:
        availability_hint = _extract_common_availability_hint(page)
        metadata["availability"] = _normalize_availability(availability_hint)

    return metadata
