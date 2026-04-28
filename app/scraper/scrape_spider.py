from __future__ import annotations

"""Spider-backed batch scraping for seed URLs without link discovery."""

import logging
from urllib.parse import urlparse

from scrapling.fetchers import AsyncDynamicSession, AsyncStealthySession, FetcherSession
from scrapling.spiders import Request, Response, SessionManager, Spider

from .fetching import (
    auto_remaining_modes,
    detect_auto_escalation_signals,
    initial_auto_fetch_mode,
    normalize_internal_fetch_mode,
)
from .inputs import parse_fetch_mode
from .domain import PriceResult
from .parsing import make_product_id, normalize_runtime_url
from .proxies import get_proxy_retry_count, get_scrape_proxy_rotator
from .registry import ADAPTERS, detect_site_key
from .resource_blocking import browser_resource_blocking_kwargs
from .service import extract_price_from_page, missing_required_result_fields

MODE_TO_SESSION_ID = {
    "http_direct": "http_direct",
    "http_proxy": "http_proxy",
    "dynamic_direct": "dynamic_direct",
    "dynamic_proxy": "dynamic_proxy",
    "stealth_direct": "stealth_direct",
    "stealth_proxy": "stealth_proxy",
    "stealth_cf_proxy": "stealth_cf",
}
_logger = logging.getLogger("scraper_api.scraper")
_PROXY_MODES = {"http_proxy", "dynamic_proxy", "stealth_proxy", "stealth_cf_proxy"}


def _is_amazon_url(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host == "amazon.com.tr" or host.startswith("amazon.") or ".amazon." in host


def _dedupe_preserving_order(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


def _balanced_spider_remaining_modes(
    current_mode: str,
    *,
    signals: set[str],
    url: str,
    remaining_modes: list[str] | None = None,
) -> list[str]:
    """Keep Amazon fallback broad enough to work, but short enough to finish."""
    planned_modes = (
        list(remaining_modes)
        if remaining_modes is not None
        else auto_remaining_modes(current_mode, signals=signals, url=url)
    )
    if not _is_amazon_url(url):
        return planned_modes

    if current_mode == "http_direct" and "amazon_captcha" in signals:
        balanced_modes: list[str] = []
        if "dynamic_proxy" in planned_modes:
            balanced_modes.append("dynamic_proxy")
        balanced_modes.extend(
            mode
            for mode in ("dynamic_direct", "stealth_direct")
            if mode in planned_modes and mode not in balanced_modes
        )
        return balanced_modes or planned_modes

    if current_mode in _PROXY_MODES:
        direct_modes = [mode for mode in planned_modes if mode not in _PROXY_MODES]
        return direct_modes or planned_modes

    return planned_modes


class PriceScrapeSpider(Spider):
    """Fetch-only spider that processes provided seed URLs without following links."""

    name = "price_scrape"
    concurrent_requests_per_domain = 2
    max_blocked_retries = 2

    def __init__(
        self,
        *,
        urls: list[str],
        timeout_seconds: int,
        selected_sites: set[str] | None,
        fetch_mode: str,
        concurrent_requests: int,
    ) -> None:
        self.start_urls = _dedupe_preserving_order(urls)
        self._timeout_seconds = timeout_seconds
        self._selected_sites = selected_sites
        self._fetch_mode = parse_fetch_mode(fetch_mode)
        self.concurrent_requests = max(1, concurrent_requests)
        super().__init__()

    def configure_sessions(self, manager: SessionManager) -> None:
        # HTTP timeout is in seconds; browser sessions use milliseconds.
        proxy_rotator = get_scrape_proxy_rotator()
        browser_proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        browser_blocking_kwargs = browser_resource_blocking_kwargs()
        browser_proxy_retries = get_proxy_retry_count() if proxy_rotator is not None else 1
        browser_direct_retries = 1
        browser_timeout_ms = max(1000, self._timeout_seconds * 1000)
        stealth_timeout_ms = max(10000, browser_timeout_ms)
        cloudflare_timeout_ms = max(30000, browser_timeout_ms)
        _logger.info(
            "scrape_spider_sessions_configured proxy_enabled=%s browser_retries=%s timeout_seconds=%s concurrency=%s",
            proxy_rotator is not None,
            browser_proxy_retries,
            self._timeout_seconds,
            self.concurrent_requests,
        )
        manager.add(
            "http",
            FetcherSession(
                timeout=self._timeout_seconds,
                retries=2,
                retry_delay=1,
                stealthy_headers=True,
                follow_redirects="safe",
            ),
        )
        manager.add(
            "http_direct",
            FetcherSession(
                timeout=self._timeout_seconds,
                retries=2,
                retry_delay=1,
                stealthy_headers=True,
                follow_redirects="safe",
            ),
        )
        if proxy_rotator is not None:
            manager.add(
                "http_proxy",
                FetcherSession(
                    timeout=self._timeout_seconds,
                    retries=browser_proxy_retries,
                    retry_delay=1,
                    stealthy_headers=True,
                    follow_redirects="safe",
                    proxy_rotator=proxy_rotator,
                ),
                lazy=True,
            )
        manager.add(
            "dynamic_direct",
            AsyncDynamicSession(
                headless=True,
                disable_resources=True,
                **browser_blocking_kwargs,
                timeout=browser_timeout_ms,
                retries=browser_direct_retries,
                retry_delay=1,
            ),
            lazy=True,
        )
        manager.add(
            "dynamic_proxy",
            AsyncDynamicSession(
                headless=True,
                disable_resources=True,
                **browser_blocking_kwargs,
                timeout=browser_timeout_ms,
                retries=browser_proxy_retries,
                retry_delay=1,
                **browser_proxy_kwargs,
            ),
            lazy=True,
        )
        manager.add(
            "stealth_direct",
            AsyncStealthySession(
                headless=True,
                disable_resources=True,
                network_idle=False,
                wait=1000,
                solve_cloudflare=False,
                block_webrtc=True,
                **browser_blocking_kwargs,
                hide_canvas=True,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                timeout=stealth_timeout_ms,
                retries=browser_direct_retries,
                retry_delay=1,
            ),
            lazy=True,
        )
        manager.add(
            "stealth_proxy",
            AsyncStealthySession(
                headless=True,
                disable_resources=True,
                network_idle=False,
                wait=1000,
                solve_cloudflare=False,
                block_webrtc=True,
                **browser_blocking_kwargs,
                hide_canvas=True,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                timeout=stealth_timeout_ms,
                retries=browser_proxy_retries,
                retry_delay=1,
                **browser_proxy_kwargs,
            ),
            lazy=True,
        )
        manager.add(
            "stealth_cf",
            AsyncStealthySession(
                headless=True,
                disable_resources=False,
                network_idle=True,
                wait=1500,
                solve_cloudflare=True,
                block_webrtc=True,
                **browser_blocking_kwargs,
                hide_canvas=True,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                timeout=cloudflare_timeout_ms,
                retries=browser_proxy_retries,
                retry_delay=1,
                **browser_proxy_kwargs,
            ),
            lazy=True,
        )

    async def start_requests(self):
        for url in self.start_urls:
            initial_mode = (
                initial_auto_fetch_mode(url)
                if self._fetch_mode == "auto"
                else normalize_internal_fetch_mode(self._fetch_mode)
            )
            initial_sid = MODE_TO_SESSION_ID[initial_mode]
            _logger.info(
                "scrape_spider_request_start url=%s fetch_mode=%s mode=%s sid=%s",
                url,
                self._fetch_mode,
                initial_mode,
                initial_sid,
            )
            yield Request(
                url,
                sid=initial_sid,
                callback=self.parse,
                meta={
                    "original_url": url,
                    "current_mode": initial_mode,
                    "remaining_modes": [],
                },
            )

    async def is_blocked(self, response: Response) -> bool:
        return "protection" in detect_auto_escalation_signals(response)

    async def retry_blocked_request(self, request: Request, response: Response) -> Request:
        if self._fetch_mode != "auto":
            _logger.info(
                "scrape_spider_blocked_retry url=%s mode=%s action=retry_same_mode reason=manual_fetch_mode",
                response.url,
                request.meta.get("current_mode", request.sid),
            )
            return request
        current_mode = str(request.meta.get("current_mode", "http"))
        signals = detect_auto_escalation_signals(response)
        remaining_modes = _balanced_spider_remaining_modes(
            current_mode,
            signals=signals,
            url=str(response.url),
        )
        _logger.info(
            "scrape_spider_attempt_plan url=%s current_mode=%s remaining_modes=%s status=%s signals=%s reason=blocked_response",
            response.url,
            current_mode,
            ",".join(remaining_modes) or "<none>",
            getattr(response, "status", None),
            ",".join(sorted(signals)) or "<none>",
        )
        if remaining_modes:
            next_mode = remaining_modes.pop(0)
            next_sid = MODE_TO_SESSION_ID[next_mode]
            _logger.info(
                "scrape_spider_auto_escalate url=%s from_mode=%s to_mode=%s next_sid=%s remaining_modes=%s reason=blocked_response status=%s signals=%s",
                response.url,
                current_mode,
                next_mode,
                next_sid,
                ",".join(remaining_modes),
                getattr(response, "status", None),
                ",".join(sorted(signals)) or "<none>",
            )
            request.sid = next_sid
            request.meta["current_mode"] = next_mode
            request.meta["remaining_modes"] = remaining_modes
            return request
        _logger.info(
            "scrape_spider_blocked_retry url=%s mode=%s action=retry_same_mode status=%s signals=%s",
            response.url,
            current_mode,
            getattr(response, "status", None),
            ",".join(sorted(signals)) or "<none>",
        )
        return request

    async def parse(self, response: Response):
        url = str(response.meta.get("original_url", response.url))
        current_mode = str(response.meta.get("current_mode", "http"))
        _logger.info(
            "scrape_spider_response_received url=%s mode=%s status=%s",
            url,
            current_mode,
            getattr(response, "status", None),
        )
        _logger.info("scrape_extract_start mode=%s url=%s", current_mode, url)
        result = extract_price_from_page(response, url, selected_sites=self._selected_sites)
        if result.success:
            _logger.info(
                "scrape_success mode=%s url=%s adapter=%s amount=%s currency=%s source=%s listing_count=%s",
                current_mode,
                url,
                result.adapter_key,
                result.amount,
                result.currency,
                result.source,
                result.listing_count,
            )
            yield {"url": url, "result": result}
            return

        _logger.info(
            "scrape_extract_failed mode=%s url=%s adapter=%s error=%s required_missing=%s",
            current_mode,
            url,
            result.adapter_key,
            result.error,
            ",".join(missing_required_result_fields(result)) or "<none>",
        )
        if self._fetch_mode == "auto":
            remaining_modes = list(response.meta.get("remaining_modes", []))
            signals = detect_auto_escalation_signals(response)
            if not remaining_modes:
                remaining_modes = _balanced_spider_remaining_modes(
                    current_mode,
                    signals=signals,
                    url=url,
                )
            else:
                remaining_modes = _balanced_spider_remaining_modes(
                    current_mode,
                    signals=signals,
                    url=url,
                    remaining_modes=remaining_modes,
                )
            _logger.info(
                "scrape_spider_attempt_plan url=%s current_mode=%s remaining_modes=%s signals=%s reason=extract_failed",
                url,
                current_mode,
                ",".join(remaining_modes) or "<none>",
                ",".join(sorted(signals)) or "<none>",
            )
            if remaining_modes:
                next_mode = remaining_modes.pop(0)
                next_sid = MODE_TO_SESSION_ID[next_mode]
                if current_mode != next_mode:
                    _logger.info(
                        "scrape_spider_followup_request url=%s from_mode=%s to_mode=%s next_sid=%s remaining_modes=%s reason=extract_failed",
                        url,
                        current_mode,
                        next_mode,
                        next_sid,
                        ",".join(remaining_modes),
                    )
                    yield Request(
                        url,
                        sid=next_sid,
                        callback=self.parse,
                        dont_filter=True,
                        meta={
                            "original_url": url,
                            "current_mode": next_mode,
                            "remaining_modes": remaining_modes,
                        },
                    )
                    return

        _logger.info("scrape_failed url=%s mode=%s error=%s", url, current_mode, result.error)
        yield {"url": url, "result": result}


def scrape_prices_with_spider(
    *,
    urls: list[str],
    timeout_seconds: int,
    selected_sites: set[str] | None,
    fetch_mode: str,
    concurrent_requests: int,
) -> list[PriceResult]:
    """Run spider-backed scraping and return per-input results in input order."""
    ordered_urls = _dedupe_preserving_order(urls)
    if not ordered_urls:
        return []

    spider = PriceScrapeSpider(
        urls=ordered_urls,
        timeout_seconds=timeout_seconds,
        selected_sites=selected_sites,
        fetch_mode=fetch_mode,
        concurrent_requests=concurrent_requests,
    )
    crawl_result = spider.start()
    by_url: dict[str, PriceResult] = {}
    for item in crawl_result.items:
        if not isinstance(item, dict):
            continue
        item_url = str(item.get("url", "")).strip()
        result = item.get("result")
        if not item_url or result is None:
            continue
        if not isinstance(result, PriceResult):
            continue
        # Prefer successful attempts over earlier failures.
        existing = by_url.get(item_url)
        if existing is None or (getattr(result, "success", False) and not getattr(existing, "success", False)):
            by_url[item_url] = result

    blocked_requests_count = int(getattr(crawl_result.stats, "blocked_requests_count", 0) or 0)
    missing_error = "blocked_by_protection" if blocked_requests_count else "fetch_failed"
    results: list[PriceResult] = []
    for url in ordered_urls:
        if url in by_url:
            results.append(by_url[url])
            continue

        runtime_url = normalize_runtime_url(url)
        site_key = detect_site_key(runtime_url)
        results.append(
            PriceResult(
                url=runtime_url,
                success=False,
                product_id=make_product_id(runtime_url),
                site_key=site_key,
                adapter_key=site_key if site_key in ADAPTERS else "generic",
                error=missing_error,
            )
        )
    return results
