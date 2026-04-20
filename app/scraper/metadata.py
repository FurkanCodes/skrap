from __future__ import annotations

"""Product metadata extraction helpers (JSON-LD -> meta tags -> DOM)."""

import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

from scrapling.parser import Selector


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


def _normalize_availability(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized.endswith("/instock") or "in stock" in normalized:
        return "in_stock"
    if normalized.endswith("/outofstock") or "out of stock" in normalized:
        return "out_of_stock"
    if normalized.endswith("/preorder") or "pre-order" in normalized:
        return "preorder"
    if normalized.endswith("/discontinued"):
        return "discontinued"
    if "stokta" in normalized:
        return "in_stock"
    if "tukendi" in normalized:
        return "out_of_stock"
    return _clean_text(value)


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
        availability_hint = _first_css(
            page,
            [
                "link[itemprop='availability']::attr(href)",
                "[itemprop='availability']::attr(href)",
                ".availability::text",
            ],
        )
        metadata["availability"] = _normalize_availability(availability_hint)

    return metadata
