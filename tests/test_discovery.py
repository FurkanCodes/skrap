from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin

import pytest
from scrapling.spiders import Request

from app.scraper import discovery as discovery_module
from app.scraper.discovery import PriceDiscoverySpider, PriceSitemapDiscoverySpider, discover_urls


@dataclass
class _FakeElement:
    href: str
    tag: str = "a"
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
class _FakeRoot:
    links: list[_FakeElement]

    def xpath(self, selector: str) -> list[str]:
        hrefs: list[str] = []
        for element in self.links:
            if f".//{element.tag}/@href" in selector:
                hrefs.append(element.href)
        return hrefs


@dataclass
class _FakeResponse:
    url: str
    meta: dict[str, Any]
    links: list[_FakeElement]
    encoding: str = "utf-8"

    @property
    def _root(self) -> _FakeRoot:
        return _FakeRoot(self.links)

    def css(self, selector: str) -> list[_FakeElement]:
        if selector != "a":
            return []
        return [link for link in self.links if link.tag == "a"]

    def urljoin(self, href: str) -> str:
        return urljoin(self.url, href)

    def follow(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return {"url": url, "kwargs": kwargs}


@dataclass
class _FakeSitemapResponse:
    url: str
    body: bytes
    headers: dict[str, str] | None = None
    encoding: str = "utf-8"

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


def test_area_product_links_are_discovered_and_assets_are_ignored() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=1,
        timeout_seconds=20,
    )
    response = _FakeResponse(
        url="https://example.com/category/laptops",
        meta={"depth": 0},
        links=[
            _FakeElement(href="/product/hotspot-1", tag="area"),
            _FakeElement(href="/product/manual.pdf", tag="area"),
        ],
    )

    yielded = asyncio.run(_collect(spider.parse(response)))

    follow_urls = [item["url"] for item in yielded if "kwargs" in item]
    assert follow_urls == ["https://example.com/product/hotspot-1"]


def test_semantic_pagination_link_is_followed_without_page_query() -> None:
    spider = PriceDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=1,
        timeout_seconds=20,
    )
    response = _FakeResponse(
        url="https://example.com/category/laptops",
        meta={"depth": 0},
        links=[_FakeElement(href="/category/laptops/2", text="Next page")],
    )

    yielded = asyncio.run(_collect(spider.parse(response)))

    follow_urls = [item["url"] for item in yielded if "kwargs" in item]
    assert follow_urls == ["https://example.com/category/laptops/2"]


def test_sitemap_discovery_seeds_robots_and_default_sitemap() -> None:
    spider = PriceSitemapDiscoverySpider(
        start_urls=["https://www.example.com/category/laptops"],
        max_pages=50,
        timeout_seconds=20,
    )

    assert spider.sitemap_urls == [
        "https://www.example.com/robots.txt",
        "https://www.example.com/sitemap.xml",
    ]


def test_sitemap_discovery_yields_product_urls_without_fetching_product_pages() -> None:
    spider = PriceSitemapDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=1,
        timeout_seconds=20,
    )
    response = _FakeSitemapResponse(
        url="https://example.com/sitemap.xml",
        headers={"content-type": "application/xml"},
        body=b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/product/laptop-1?utm_source=newsletter</loc></url>
          <url><loc>https://example.com/item/laptop-2</loc></url>
          <url><loc>https://example.com/product/manual.pdf</loc></url>
          <url><loc>https://other.example.org/product/laptop-3</loc></url>
        </urlset>""",
    )

    yielded = asyncio.run(_collect(spider._parse_sitemap(response)))

    assert yielded == [{"url": "https://example.com/product/laptop-1"}]


def test_robots_sitemap_directives_follow_same_domain_sitemaps() -> None:
    spider = PriceSitemapDiscoverySpider(
        start_urls=["https://example.com/category/laptops"],
        max_pages=5,
        timeout_seconds=20,
    )
    response = _FakeSitemapResponse(
        url="https://example.com/robots.txt",
        body=(
            b"Sitemap: https://example.com/sitemap-products.xml\n"
            b"Sitemap: https://other.example.org/sitemap.xml\n"
        ),
    )

    yielded = asyncio.run(_collect(spider._parse_sitemap(response)))

    assert yielded == [
        {
            "url": "https://example.com/sitemap-products.xml",
            "kwargs": {"callback": spider._parse_sitemap, "sid": "http"},
        }
    ]


def _fake_crawl_result(items: list[dict[str, str]]):
    return SimpleNamespace(
        items=items,
        stats=SimpleNamespace(
            requests_count=1,
            failed_requests_count=0,
            blocked_requests_count=0,
            offsite_requests_count=0,
            elapsed_seconds=0.01,
            sessions_requests_count={"http": 1},
        ),
    )


def test_discover_urls_uses_sitemap_results_without_html_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSitemapSpider:
        def __init__(self, **_: Any) -> None:
            pass

        def start(self):
            return _fake_crawl_result([{"url": "https://example.com/product/laptop-1"}])

    class _UnexpectedHtmlSpider:
        def __init__(self, **_: Any) -> None:
            pass

        def start(self):  # pragma: no cover - should not be called
            raise AssertionError("HTML fallback should not run when sitemap finds URLs")

    monkeypatch.setattr(discovery_module, "PriceSitemapDiscoverySpider", _FakeSitemapSpider)
    monkeypatch.setattr(discovery_module, "PriceDiscoverySpider", _UnexpectedHtmlSpider)

    result = discover_urls(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=2,
        timeout_seconds=20,
    )

    assert result.discovered_urls == ["https://example.com/product/laptop-1"]


def test_discover_urls_falls_back_to_html_when_sitemap_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSitemapSpider:
        def __init__(self, **_: Any) -> None:
            pass

        def start(self):
            return _fake_crawl_result([])

    class _FakeHtmlSpider:
        def __init__(self, **_: Any) -> None:
            pass

        def start(self):
            return _fake_crawl_result([{"url": "https://example.com/category/laptops"}])

    monkeypatch.setattr(discovery_module, "PriceSitemapDiscoverySpider", _FakeSitemapSpider)
    monkeypatch.setattr(discovery_module, "PriceDiscoverySpider", _FakeHtmlSpider)

    result = discover_urls(
        start_urls=["https://example.com/category/laptops"],
        max_pages=50,
        max_depth=2,
        timeout_seconds=20,
    )

    assert result.discovered_urls == ["https://example.com/category/laptops"]


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
