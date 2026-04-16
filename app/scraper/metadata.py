from __future__ import annotations

"""Product metadata extraction helpers (JSON-LD -> meta tags -> DOM)."""

import json
from typing import Any, Iterator
from urllib.parse import urljoin

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

    canonical_url = metadata["canonical_url"] or _first_css(
        page,
        [
            "link[rel='canonical']::attr(href)",
            "meta[property='og:url']::attr(content)",
        ],
    )
    metadata["canonical_url"] = urljoin(source_url, canonical_url) if canonical_url else source_url

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
