from __future__ import annotations

"""Fetcher selection and auto-escalation heuristics."""

import logging
from urllib.parse import urlparse

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
from scrapling.parser import Selector

from .constants import CLOUDFLARE_HINTS, JAVASCRIPT_HINTS, PROTECTION_HINTS, PROTECTION_STATUS_CODES
from .proxies import get_proxy_retry_count, get_scrape_proxy_rotator
from .resource_blocking import browser_resource_blocking_kwargs

_logger = logging.getLogger("scraper_api.scraper")

EXTERNAL_TO_INTERNAL_MODE = {
    "http": "http_direct",
    "dynamic": "dynamic_proxy",
    "stealth": "stealth_proxy",
}
AMAZON_CAPTCHA_HINTS = (
    "opfcaptcha.amazon",
    "/errors/validatecaptcha",
    "validatecaptcha",
)


def _is_amazon_url(url: str | None) -> bool:
    if not url:
        return False
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host == "amazon.com.tr" or host.startswith("amazon.") or ".amazon." in host


def initial_auto_fetch_mode(url: str) -> str:
    """Choose the first transport for auto mode."""
    _logger.info("scrape_auto_initial_mode mode=http_direct reason=sequential_probe url=%s", url)
    return "http_direct"


def normalize_internal_fetch_mode(mode: str) -> str:
    return EXTERNAL_TO_INTERNAL_MODE.get(mode, mode)


def auto_remaining_modes(
    current_mode: str,
    *,
    signals: set[str] | None = None,
    proxy_enabled: bool | None = None,
    url: str | None = None,
) -> list[str]:
    """Return untried auto-mode fallbacks in the best order for observed signals."""
    normalized_mode = normalize_internal_fetch_mode(current_mode)
    seen = {normalized_mode}
    has_proxy = get_scrape_proxy_rotator() is not None if proxy_enabled is None else proxy_enabled
    observed = signals or set()

    if _is_amazon_url(url) and ("protection" in observed or normalized_mode != "http_direct"):
        ordered = ["dynamic_proxy", "stealth_proxy", "dynamic_direct", "stealth_direct"]
    elif "cloudflare" in observed:
        ordered = ["http_proxy", "stealth_cf_proxy", "stealth_proxy", "dynamic_proxy"]
    elif "protection" in observed:
        ordered = ["http_proxy", "stealth_proxy", "dynamic_proxy"]
    elif "javascript" in observed:
        ordered = ["dynamic_proxy", "stealth_proxy"]
    else:
        ordered = ["dynamic_proxy", "stealth_proxy"]

    if has_proxy and normalized_mode == "http_direct" and not _is_amazon_url(url):
        ordered = ["http_proxy", *ordered]
    if not has_proxy:
        direct_mode_for_proxy_mode = {
            "dynamic_proxy": "dynamic_direct",
            "stealth_proxy": "stealth_direct",
        }
        ordered = [
            direct_mode_for_proxy_mode.get(mode, mode)
            for mode in ordered
            if mode != "http_proxy"
        ]
    remaining: list[str] = []
    for mode in ordered:
        if mode in seen or mode in remaining:
            continue
        remaining.append(mode)
    return remaining


def fetch_page_with_mode(url: str, timeout: int, mode: str) -> Selector:
    """Fetch a page using the requested fetch transport mode.

    - `http`: lightweight HTTP fetcher
    - `dynamic`: Playwright-backed browser fetcher
    - `stealth`: hardened browser fetcher for protected targets
    """
    _logger.info("scrape_fetch_start mode=%s url=%s timeout_seconds=%s", mode, url, timeout)
    internal_mode = normalize_internal_fetch_mode(mode)
    if internal_mode in {"http_direct", "http_proxy"}:
        proxy_rotator = get_scrape_proxy_rotator() if internal_mode == "http_proxy" else None
        proxy_kwargs = {"proxy": proxy_rotator.get_proxy()} if proxy_rotator is not None else {}
        if proxy_rotator is not None:
            _logger.info("scrape_fetch_http_proxy_config proxy_enabled=True url=%s", url)
        page = Fetcher.get(
            url,
            timeout=timeout,
            retries=2,
            retry_delay=1,
            stealthy_headers=True,
            follow_redirects="safe",
            **proxy_kwargs,
        )
        _logger.info("scrape_fetch_complete mode=%s status=%s url=%s", internal_mode, getattr(page, "status", None), url)
        return page

    browser_timeout_ms = max(1000, timeout * 1000)
    stealth_timeout_ms = max(10000, browser_timeout_ms)
    cloudflare_timeout_ms = max(30000, browser_timeout_ms)
    base_kwargs = {
        "timeout": browser_timeout_ms,
        "retries": 1,
        "retry_delay": 1,
        "headless": True,
        "disable_resources": True,
        **browser_resource_blocking_kwargs(),
    }

    if internal_mode in {"dynamic_direct", "dynamic_proxy"}:
        proxy_rotator = get_scrape_proxy_rotator() if internal_mode == "dynamic_proxy" else None
        proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        retries = get_proxy_retry_count() if proxy_rotator is not None else 1
        _logger.info(
            "scrape_fetch_dynamic_config mode=%s proxy_enabled=%s retries=%s url=%s",
            internal_mode,
            proxy_rotator is not None,
            retries,
            url,
        )
        dynamic_kwargs = {
            **base_kwargs,
            "retries": retries,
            **proxy_kwargs,
        }
        page = DynamicFetcher.fetch(url, **dynamic_kwargs)
        _logger.info("scrape_fetch_complete mode=%s status=%s url=%s", internal_mode, getattr(page, "status", None), url)
        return page
    if internal_mode in {"stealth_direct", "stealth_proxy", "stealth_cf_proxy"}:
        proxy_rotator = get_scrape_proxy_rotator() if internal_mode in {"stealth_proxy", "stealth_cf_proxy"} else None
        proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        solve_cloudflare = internal_mode == "stealth_cf_proxy"
        retries = get_proxy_retry_count() if proxy_rotator is not None else 1
        _logger.info(
            "scrape_fetch_stealth_config mode=%s proxy_enabled=%s retries=%s solve_cloudflare=%s url=%s",
            internal_mode,
            proxy_rotator is not None,
            retries,
            solve_cloudflare,
            url,
        )
        stealth_kwargs = {
            **base_kwargs,
            "timeout": cloudflare_timeout_ms if solve_cloudflare else stealth_timeout_ms,
            "retries": retries,
            "disable_resources": not solve_cloudflare,
            "network_idle": solve_cloudflare,
            "wait": 1500 if solve_cloudflare else 1000,
            "solve_cloudflare": solve_cloudflare,
            "block_webrtc": True,
            "hide_canvas": True,
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
            **proxy_kwargs,
        }
        page = StealthyFetcher.fetch(
            url,
            **stealth_kwargs,
        )
        _logger.info("scrape_fetch_complete mode=%s status=%s url=%s", internal_mode, getattr(page, "status", None), url)
        return page

    raise ValueError(f"Unsupported fetch mode '{mode}'")


def detect_auto_escalation_signals(page: Selector, *, url: str | None = None) -> set[str]:
    """Detect whether HTTP results indicate JS/protection escalation is needed."""
    signals: set[str] = set()

    status = getattr(page, "status", None)
    if isinstance(status, int) and status in PROTECTION_STATUS_CODES:
        signals.add("protection")

    page_url = url or str(getattr(page, "url", "") or "")
    page_text = str(page.get_all_text(separator=" ", strip=True)).lower()
    html_text = str(getattr(page, "html_content", "") or "").lower()
    if any(token in html_text for token in AMAZON_CAPTCHA_HINTS) or (
        _is_amazon_url(page_url) and "captcha" in html_text and "amazon" in html_text
    ):
        signals.add("amazon_captcha")
        signals.add("protection")
    if any(token in page_text for token in PROTECTION_HINTS):
        signals.add("protection")
    if any(token in page_text for token in CLOUDFLARE_HINTS):
        signals.add("cloudflare")
        signals.add("protection")
    if any(token in page_text for token in JAVASCRIPT_HINTS):
        signals.add("javascript")

    if len(page_text) < 200 and len(page.css("script")) >= 15:
        signals.add("javascript")

    noscript_text = " ".join(page.css("noscript::text").getall()).lower()
    if "javascript" in noscript_text:
        signals.add("javascript")

    return signals


def auto_followup_modes(signals: set[str]) -> list[str]:
    """Choose follow-up modes in preferred order for auto mode."""
    return auto_remaining_modes("http_direct", signals=signals)
