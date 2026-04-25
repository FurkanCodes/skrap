from __future__ import annotations

"""Spider-based URL discovery for product crawling."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from scrapling.fetchers import AsyncStealthySession, FetcherSession
from scrapling.spiders import Request, Response, SessionManager, Spider

from .parsing import strip_tracking_query_params
from .proxies import get_proxy_retry_count, get_scrape_proxy_rotator

PAGINATION_TEXT_TOKENS = {
    "next",
    "next page",
    "older",
    "load more",
    "more",
    "ileri",
    "sonraki",
    "»",
    "›",
    ">",
}

PAGINATION_ATTR_TOKENS = {
    "pagination",
    "pager",
    "next",
    "page-numbers",
}

PAGINATION_PATH_TOKENS = {
    "/page/",
}

PRODUCT_PATH_TOKENS = {
    "/product/",
    "/products/",
    "/item/",
    "/items/",
    "/dp/",
    "/p/",
    "/sku/",
    "/urun/",
    "/detail/",
}

PRODUCT_QUERY_KEYS = {
    "product",
    "product_id",
    "item",
    "item_id",
    "sku",
    "id",
}

DENY_PATH_TOKENS = {
    "/cart",
    "/checkout",
    "/login",
    "/signin",
    "/register",
    "/account",
    "/wishlist",
    "/compare",
    "/help",
    "/faq",
    "/privacy",
    "/terms",
    "/contact",
    "/about",
    "/blog",
    "/news",
}


@dataclass(frozen=True)
class DiscoveryStats:
    requests_count: int
    failed_requests_count: int
    blocked_requests_count: int
    offsite_requests_count: int
    elapsed_seconds: float
    sessions_requests_count: dict[str, int]


@dataclass(frozen=True)
class DiscoveryRunResult:
    discovered_urls: list[str]
    stats: DiscoveryStats


class PriceDiscoverySpider(Spider):
    """Spider that discovers product-like URLs from seed pages."""

    name = "price_discovery"
    concurrent_requests = 4
    concurrent_requests_per_domain = 2
    download_delay = 0.0
    max_blocked_retries = 3

    def __init__(
        self,
        *,
        start_urls: list[str],
        max_pages: int,
        max_depth: int,
        timeout_seconds: int,
    ) -> None:
        self.start_urls = list(dict.fromkeys(start_urls))
        self.allowed_domains = self._derive_allowed_domains(self.start_urls)
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._timeout_seconds = timeout_seconds
        self._visited_pages = 0
        self._discovered_urls: list[str] = []
        self._seen_discovered: set[str] = set()
        self._seed_canonical_urls = {self._canonicalize(url) for url in self.start_urls}
        super().__init__()

    @staticmethod
    def _derive_allowed_domains(start_urls: list[str]) -> set[str]:
        domains: set[str] = set()
        for url in start_urls:
            host = urlparse(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                domains.add(host)
        return domains

    def configure_sessions(self, manager: SessionManager) -> None:
        # Timeout units differ across fetchers, so set them per session type.
        proxy_rotator = get_scrape_proxy_rotator()
        stealth_proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        stealth_retries = get_proxy_retry_count()
        manager.add(
            "http",
            FetcherSession(timeout=self._timeout_seconds, retries=2, retry_delay=1),
        )
        manager.add(
            "stealth",
            AsyncStealthySession(
                headless=True,
                disable_resources=False,
                network_idle=True,
                wait=1500,
                block_webrtc=True,
                solve_cloudflare=True,
                hide_canvas=True,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                timeout=max(60000, self._timeout_seconds * 1000),
                retries=stealth_retries,
                retry_delay=1,
                **stealth_proxy_kwargs,
            ),
            lazy=True,
        )

    async def start_requests(self):
        for url in self.start_urls:
            yield Request(
                url,
                sid="http",
                callback=self.parse,
                meta={"depth": 0},
            )

    async def is_blocked(self, response: Response) -> bool:
        body_text = response.body.decode("utf-8", errors="ignore").lower()
        if response.status in {401, 403, 407, 429, 444, 500, 502, 503, 504}:
            return True
        return any(token in body_text for token in ("captcha", "cloudflare", "access denied"))

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        request.sid = "stealth"
        return request

    def _is_http_url(self, url: str) -> bool:
        scheme = urlparse(url).scheme.lower()
        return scheme in {"http", "https"}

    def _is_same_domain(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains)

    def _canonicalize(self, url: str) -> str:
        return strip_tracking_query_params(url)

    def _is_deny_path(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(token in path for token in DENY_PATH_TOKENS)

    def _looks_like_pagination_url(self, url: str, text: str, rel: str, class_id: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if rel in {"next", "prev"}:
            return True
        if any(token in text for token in PAGINATION_TEXT_TOKENS):
            return True
        if any(token in class_id for token in PAGINATION_ATTR_TOKENS):
            return True
        if any(token in path for token in PAGINATION_PATH_TOKENS):
            return True
        return "page" in query

    def _looks_like_product_url(self, url: str) -> bool:
        if self._is_deny_path(url):
            return False
        parsed = urlparse(url)
        path = parsed.path.lower()
        if any(token in path for token in PRODUCT_PATH_TOKENS):
            return True
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        return any(key in PRODUCT_QUERY_KEYS for key in query)

    def _should_expand(self, depth: int) -> bool:
        return depth < self._max_depth and self._visited_pages < self._max_pages

    async def parse(self, response: Response):
        if self._visited_pages >= self._max_pages:
            return
        self._visited_pages += 1

        current_depth = int(response.meta.get("depth", 0))
        canonical_current = self._canonicalize(response.url)
        is_seed_url = canonical_current in self._seed_canonical_urls

        # Always keep seed URLs as discovery candidates so product pages with
        # unfamiliar URL patterns can still flow into phase-2 price extraction.
        if (is_seed_url or self._looks_like_product_url(canonical_current)) and canonical_current not in self._seen_discovered:
            self._seen_discovered.add(canonical_current)
            self._discovered_urls.append(canonical_current)
            yield {"url": canonical_current}

        if not self._should_expand(current_depth):
            return

        seen_links_on_page: set[str] = set()
        next_depth = current_depth + 1
        for element in response.css("a"):
            href = str(element.attrib.get("href", "")).strip()
            if not href:
                continue
            absolute = response.urljoin(href)
            canonical = self._canonicalize(absolute)
            if not canonical:
                continue
            if canonical in seen_links_on_page:
                continue
            seen_links_on_page.add(canonical)
            if not self._is_http_url(canonical) or not self._is_same_domain(canonical):
                continue
            if self._is_deny_path(canonical):
                continue

            text = str(element.get_all_text(separator=" ", strip=True)).lower()
            rel = str(element.attrib.get("rel", "")).lower()
            class_id = f"{element.attrib.get('class', '')} {element.attrib.get('id', '')}".lower()
            is_pagination = self._looks_like_pagination_url(canonical, text=text, rel=rel, class_id=class_id)
            is_product = self._looks_like_product_url(canonical)
            if not (is_pagination or is_product):
                continue

            yield response.follow(
                canonical,
                callback=self.parse,
                sid="http",
                priority=10 if is_pagination else 5,
                meta={"depth": next_depth},
            )


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_discovery_stats(crawl_stats: Any) -> DiscoveryStats:
    elapsed_seconds_attr = getattr(crawl_stats, "elapsed_seconds", None)
    if elapsed_seconds_attr is not None:
        elapsed_seconds = max(0.0, float(elapsed_seconds_attr))
    else:
        start_time = float(getattr(crawl_stats, "start_time", 0.0) or 0.0)
        end_time = float(getattr(crawl_stats, "end_time", 0.0) or 0.0)
        elapsed_seconds = max(0.0, end_time - start_time)
    return DiscoveryStats(
        requests_count=_safe_int(getattr(crawl_stats, "requests_count", 0)),
        failed_requests_count=_safe_int(getattr(crawl_stats, "failed_requests_count", 0)),
        blocked_requests_count=_safe_int(getattr(crawl_stats, "blocked_requests_count", 0)),
        offsite_requests_count=_safe_int(getattr(crawl_stats, "offsite_requests_count", 0)),
        elapsed_seconds=elapsed_seconds,
        sessions_requests_count=dict(getattr(crawl_stats, "sessions_requests_count", {}) or {}),
    )


def discover_urls(
    *,
    start_urls: list[str],
    max_pages: int,
    max_depth: int,
    timeout_seconds: int,
) -> DiscoveryRunResult:
    """Run discovery spider synchronously and return discovered URLs + stats."""
    spider = PriceDiscoverySpider(
        start_urls=start_urls,
        max_pages=max_pages,
        max_depth=max_depth,
        timeout_seconds=timeout_seconds,
    )
    crawl_result = spider.start()
    discovered = [
        str(item.get("url", "")).strip()
        for item in crawl_result.items
        if isinstance(item, dict) and str(item.get("url", "")).strip()
    ]
    deduped_urls = _dedupe_preserving_order(discovered)
    return DiscoveryRunResult(
        discovered_urls=deduped_urls,
        stats=_to_discovery_stats(crawl_result.stats),
    )
