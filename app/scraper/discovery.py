from __future__ import annotations

"""Spider-based URL discovery for product crawling."""

from dataclasses import dataclass
import logging
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from scrapling.fetchers import AsyncStealthySession, FetcherSession
from scrapling.spiders import LinkExtractor, Request, Response, SessionManager, SitemapSpider, Spider

from .parsing import strip_tracking_query_params
from .proxies import get_proxy_retry_count, get_scrape_proxy_rotator
from .resource_blocking import browser_resource_blocking_kwargs

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

_DENY_LINK_PATTERN = "|".join(re.escape(token) for token in sorted(DENY_PATH_TOKENS))
_PRODUCT_LINK_PATTERN = "|".join(
    (
        *(re.escape(token) for token in sorted(PRODUCT_PATH_TOKENS)),
        rf"[?&](?:{'|'.join(re.escape(key) for key in sorted(PRODUCT_QUERY_KEYS))})=",
    )
)
_PAGINATION_LINK_PATTERN = "|".join(
    (
        *(re.escape(token) for token in sorted(PAGINATION_PATH_TOKENS)),
        r"[?&]page=",
    )
)
_logger = logging.getLogger("scraper_api.discovery")


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


def _derive_allowed_domains(start_urls: list[str]) -> set[str]:
    domains: set[str] = set()
    for url in start_urls:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            domains.add(host)
    return domains


def _is_http_url(url: str) -> bool:
    scheme = urlparse(url).scheme.lower()
    return scheme in {"http", "https"}


def _is_same_domain(url: str, allowed_domains: set[str]) -> bool:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def _sitemap_seed_urls(start_urls: list[str]) -> list[str]:
    seed_urls: list[str] = []
    seen_origins: set[tuple[str, str]] = set()
    for raw_url in start_urls:
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue

        normalized_path = parsed.path.lower()
        if normalized_path.endswith("/robots.txt") or "sitemap" in normalized_path:
            seed_urls.append(urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, "")))

        origin = (parsed.scheme, parsed.netloc)
        if origin in seen_origins:
            continue
        seen_origins.add(origin)
        seed_urls.append(urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", "")))
        seed_urls.append(urlunparse((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "", "")))

    return _dedupe_preserving_order(seed_urls)


class PriceSitemapDiscoverySpider(SitemapSpider):
    """Sitemap/robots discovery that emits product-like URLs without fetching them."""

    name = "price_sitemap_discovery"
    concurrent_requests = 4
    concurrent_requests_per_domain = 2
    download_delay = 0.0

    def __init__(
        self,
        *,
        start_urls: list[str],
        max_pages: int,
        timeout_seconds: int,
    ) -> None:
        self.start_urls = list(dict.fromkeys(start_urls))
        self.sitemap_urls = _sitemap_seed_urls(self.start_urls)
        self.allowed_domains = _derive_allowed_domains(self.start_urls)
        self._max_pages = max_pages
        self._max_sitemap_requests = max(10, max_pages)
        self._timeout_seconds = timeout_seconds
        self._visited_sitemaps = 0
        self._seen_discovered: set[str] = set()
        self._product_links = LinkExtractor(
            allow=_PRODUCT_LINK_PATTERN,
            allow_domains=self.allowed_domains,
            deny=_DENY_LINK_PATTERN,
            process=self._canonicalize,
        )
        self.sitemap_follow = LinkExtractor(
            allow_domains=self.allowed_domains,
            process=self._canonicalize,
        )
        super().__init__()

    def configure_sessions(self, manager: SessionManager) -> None:
        manager.add(
            "http",
            FetcherSession(timeout=self._timeout_seconds, retries=1, retry_delay=1),
        )

    async def start_requests(self):
        for url in self.sitemap_urls:
            yield Request(
                url,
                sid="http",
                callback=self._parse_sitemap,
            )

    def _canonicalize(self, url: str) -> str:
        return strip_tracking_query_params(url)

    def _should_follow_sitemap(self, url: str) -> bool:
        canonical = self._canonicalize(url)
        return (
            self._visited_sitemaps < self._max_sitemap_requests
            and _is_http_url(canonical)
            and _is_same_domain(canonical, self.allowed_domains)
            and (self.sitemap_follow is None or self.sitemap_follow.matches(canonical))
        )

    def _should_discover_url(self, url: str) -> bool:
        canonical = self._canonicalize(url)
        return (
            len(self._seen_discovered) < self._max_pages
            and _is_http_url(canonical)
            and self._product_links.matches(canonical)
            and canonical not in self._seen_discovered
        )

    async def _parse_sitemap(self, response: Response):
        if self._visited_sitemaps >= self._max_sitemap_requests:
            return
        self._visited_sitemaps += 1

        if urlparse(str(response.url)).path.endswith("/robots.txt"):
            for sitemap_url in self._robots_body(response):
                if self._should_follow_sitemap(sitemap_url):
                    yield response.follow(sitemap_url, callback=self._parse_sitemap, sid="http")
            return

        content_type = response.headers.get("content-type") if response.headers else None
        result = self._sm_body(response.body, content_type=content_type)

        for child_url in result.sitemaps:
            if len(self._seen_discovered) >= self._max_pages:
                return
            if self._should_follow_sitemap(child_url):
                yield response.follow(child_url, callback=self._parse_sitemap, sid="http")

        for url in result.urls:
            if not self._should_discover_url(url):
                continue
            canonical = self._canonicalize(url)
            self._seen_discovered.add(canonical)
            yield {"url": canonical}


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
        self._same_domain_links = LinkExtractor(
            allow_domains=self.allowed_domains,
            deny=_DENY_LINK_PATTERN,
            process=self._canonicalize,
        )
        self._product_links = LinkExtractor(
            allow=_PRODUCT_LINK_PATTERN,
            allow_domains=self.allowed_domains,
            deny=_DENY_LINK_PATTERN,
            process=self._canonicalize,
        )
        self._pagination_links = LinkExtractor(
            allow=_PAGINATION_LINK_PATTERN,
            allow_domains=self.allowed_domains,
            deny=_DENY_LINK_PATTERN,
            process=self._canonicalize,
        )
        super().__init__()

    @staticmethod
    def _derive_allowed_domains(start_urls: list[str]) -> set[str]:
        return _derive_allowed_domains(start_urls)

    def configure_sessions(self, manager: SessionManager) -> None:
        # Timeout units differ across fetchers, so set them per session type.
        proxy_rotator = get_scrape_proxy_rotator()
        stealth_proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        browser_blocking_kwargs = browser_resource_blocking_kwargs()
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
                **browser_blocking_kwargs,
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
        return _is_http_url(url)

    def _is_same_domain(self, url: str) -> bool:
        return _is_same_domain(url, self.allowed_domains)

    def _canonicalize(self, url: str) -> str:
        return strip_tracking_query_params(url)

    def _is_deny_path(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(token in path for token in DENY_PATH_TOKENS)

    def _looks_like_pagination_url(self, url: str, text: str, rel: str, class_id: str) -> bool:
        if rel in {"next", "prev"}:
            return True
        if any(token in text for token in PAGINATION_TEXT_TOKENS):
            return True
        if any(token in class_id for token in PAGINATION_ATTR_TOKENS):
            return True
        return self._pagination_links.matches(url)

    def _looks_like_product_url(self, url: str) -> bool:
        return self._product_links.matches(url)

    def _should_expand(self, depth: int) -> bool:
        return depth < self._max_depth and self._visited_pages < self._max_pages

    def _candidate_links(self, response: Response) -> list[tuple[str, bool, bool]]:
        """Return same-domain links with product/pagination classification."""
        candidates: dict[str, dict[str, bool]] = {}
        for canonical in self._same_domain_links.extract(response):
            candidates[canonical] = {
                "is_pagination": self._looks_like_pagination_url(canonical, text="", rel="", class_id=""),
                "is_product": self._looks_like_product_url(canonical),
            }

        for element in response.css("a"):
            href = str(element.attrib.get("href", "")).strip()
            if not href:
                continue
            canonical = self._canonicalize(response.urljoin(href))
            if not self._same_domain_links.matches(canonical):
                continue

            text = str(element.get_all_text(separator=" ", strip=True)).lower()
            rel = str(element.attrib.get("rel", "")).lower()
            class_id = f"{element.attrib.get('class', '')} {element.attrib.get('id', '')}".lower()
            if not self._looks_like_pagination_url(canonical, text=text, rel=rel, class_id=class_id):
                continue

            flags = candidates.setdefault(
                canonical,
                {
                    "is_pagination": False,
                    "is_product": self._looks_like_product_url(canonical),
                },
            )
            flags["is_pagination"] = True

        return [
            (url, flags["is_pagination"], flags["is_product"])
            for url, flags in candidates.items()
            if flags["is_pagination"] or flags["is_product"]
        ]

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

        next_depth = current_depth + 1
        for canonical, is_pagination, _is_product in self._candidate_links(response):
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
    sitemap_spider = PriceSitemapDiscoverySpider(
        start_urls=start_urls,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
    )
    try:
        sitemap_result = sitemap_spider.start()
    except Exception as exc:  # pragma: no cover - defensive guard around network spider runtime
        _logger.info("sitemap_discovery_failed seed_count=%s error=%s", len(start_urls), exc)
    else:
        sitemap_discovered = [
            str(item.get("url", "")).strip()
            for item in sitemap_result.items
            if isinstance(item, dict) and str(item.get("url", "")).strip()
        ]
        sitemap_urls = _dedupe_preserving_order(sitemap_discovered)[:max_pages]
        if sitemap_urls:
            _logger.info(
                "sitemap_discovery_complete seed_count=%s discovered_url_count=%s",
                len(start_urls),
                len(sitemap_urls),
            )
            return DiscoveryRunResult(
                discovered_urls=sitemap_urls,
                stats=_to_discovery_stats(sitemap_result.stats),
            )

        _logger.info("sitemap_discovery_empty seed_count=%s fallback=html", len(start_urls))

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
