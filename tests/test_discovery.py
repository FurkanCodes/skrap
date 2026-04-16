from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from scrapling.spiders import Request

from app.scraper.discovery import PriceDiscoverySpider


@dataclass
class _FakeElement:
    href: str
    text: str = ""
    rel: str = ""
    class_name: str = ""
    element_id: str = ""

    @property
    def attrib(self) -> dict[str, str]:
        return {
            "href": self.href,
            "rel": self.rel,
            "class": self.class_name,
            "id": self.element_id,
        }

    def get_all_text(self, separator: str = " ", strip: bool = True) -> str:
        return self.text


@dataclass
class _FakeResponse:
    url: str
    meta: dict[str, Any]
    links: list[_FakeElement]

    def css(self, selector: str) -> list[_FakeElement]:
        if selector != "a":
            return []
        return self.links

    def urljoin(self, href: str) -> str:
        return urljoin(self.url, href)

    def follow(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return {"url": url, "kwargs": kwargs}


async def _collect(async_iterable) -> list[Any]:
    values: list[Any] = []
    async for item in async_iterable:
        values.append(item)
    return values


def test_same_domain_filtering() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://www.example.com/category/laptops"],
        max_pages=50,
        max_depth=2,
        timeout_seconds=20,
    )

    assert spider._is_same_domain("https://example.com/product/1")
    assert spider._is_same_domain("https://shop.example.com/product/2")
    assert not spider._is_same_domain("https://another-site.com/product/3")


def test_pagination_and_product_link_heuristics() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=2,
        timeout_seconds=20,
    )

    assert spider._looks_like_pagination_url(
        "https://example.com/category/laptops?page=2",
        text="",
        rel="",
        class_id="",
    )
    assert spider._looks_like_pagination_url(
        "https://example.com/category/laptops",
        text="next page",
        rel="",
        class_id="",
    )
    assert spider._looks_like_product_url("https://example.com/product/laptop-1")
    assert spider._looks_like_product_url("https://example.com/search?product_id=123")
    assert not spider._looks_like_product_url("https://example.com/cart")


def test_depth_budget_prevents_follow_requests() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=0,
        timeout_seconds=20,
    )
    response = _FakeResponse(
        url="https://example.com/category/laptops",
        meta={"depth": 0},
        links=[_FakeElement(href="/product/laptop-1", text="Laptop 1")],
    )

    yielded = asyncio.run(_collect(spider.parse(response)))

    assert yielded == [{"url": "https://example.com/category/laptops"}]


def test_page_budget_stops_discovery_expansion() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=2,
        max_depth=2,
        timeout_seconds=20,
    )

    first_response = _FakeResponse(
        url="https://example.com/category/laptops",
        meta={"depth": 0},
        links=[_FakeElement(href="/product/laptop-1", text="Laptop 1")],
    )
    second_response = _FakeResponse(
        url="https://example.com/category/laptops?page=2",
        meta={"depth": 1},
        links=[_FakeElement(href="/product/laptop-2", text="Laptop 2")],
    )
    third_response = _FakeResponse(
        url="https://example.com/category/laptops?page=3",
        meta={"depth": 1},
        links=[_FakeElement(href="/product/laptop-3", text="Laptop 3")],
    )

    first_yielded = asyncio.run(_collect(spider.parse(first_response)))
    second_yielded = asyncio.run(_collect(spider.parse(second_response)))
    third_yielded = asyncio.run(_collect(spider.parse(third_response)))

    follow_requests = [item for item in first_yielded if "kwargs" in item]
    assert len(follow_requests) == 1
    assert follow_requests[0]["kwargs"]["meta"]["depth"] == 1
    assert second_yielded == []
    assert third_yielded == []


def test_seed_url_is_discovered_even_when_product_heuristic_misses() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/akilli-telefonlar/huawei-pura-70.html"],
        max_pages=50,
        max_depth=0,
        timeout_seconds=20,
    )
    response = _FakeResponse(
        url="https://example.com/akilli-telefonlar/huawei-pura-70.html",
        meta={"depth": 0},
        links=[],
    )

    yielded = asyncio.run(_collect(spider.parse(response)))

    assert yielded == [{"url": "https://example.com/akilli-telefonlar/huawei-pura-70.html"}]


def test_blocked_retry_switches_sid_to_stealth() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=2,
        timeout_seconds=20,
    )
    request = Request("https://example.com/product/1", sid="http")

    updated = asyncio.run(spider.retry_blocked_request(request, response=None))

    assert updated.sid == "stealth"
