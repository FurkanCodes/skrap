from __future__ import annotations

"""Fetcher selection and auto-escalation heuristics."""

import logging

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
from scrapling.parser import Selector

from .constants import CLOUDFLARE_HINTS, JAVASCRIPT_HINTS, PROTECTION_HINTS, PROTECTION_STATUS_CODES
from .proxies import get_proxy_retry_count, get_scrape_proxy_rotator

_logger = logging.getLogger("scraper_api.scraper")

EXTERNAL_TO_INTERNAL_MODE = {
    "http": "http_direct",
    "dynamic": "dynamic_proxy",
    "stealth": "stealth_proxy",
}


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
) -> list[str]:
    """Return untried auto-mode fallbacks in the best order for observed signals."""
    normalized_mode = normalize_internal_fetch_mode(current_mode)
    seen = {normalized_mode}
    has_proxy = get_scrape_proxy_rotator() is not None if proxy_enabled is None else proxy_enabled
    observed = signals or set()

    if "cloudflare" in observed:
        ordered = ["http_proxy", "stealth_cf_proxy", "stealth_proxy", "dynamic_proxy"]
    elif "protection" in observed:
        ordered = ["http_proxy", "stealth_proxy", "dynamic_proxy"]
    elif "javascript" in observed:
        ordered = ["dynamic_proxy", "stealth_proxy"]
    else:
        ordered = ["dynamic_proxy", "stealth_proxy"]

    if has_proxy and normalized_mode == "http_direct":
        ordered = ["http_proxy", *ordered]
    if not has_proxy:
        ordered = [mode for mode in ordered if mode != "http_proxy"]
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
    base_kwargs = {
        "timeout": browser_timeout_ms,
        "retries": 2,
        "retry_delay": 1,
        "headless": True,
        "disable_resources": True,
    }

    if internal_mode == "dynamic_proxy":
        proxy_rotator = get_scrape_proxy_rotator()
        proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        _logger.info(
            "scrape_fetch_dynamic_config proxy_enabled=%s retries=%s url=%s",
            proxy_rotator is not None,
            get_proxy_retry_count(),
            url,
        )
        dynamic_kwargs = {
            **base_kwargs,
            "retries": get_proxy_retry_count(),
            **proxy_kwargs,
        }
        page = DynamicFetcher.fetch(url, **dynamic_kwargs)
        _logger.info("scrape_fetch_complete mode=dynamic_proxy status=%s url=%s", getattr(page, "status", None), url)
        return page
    if internal_mode in {"stealth_proxy", "stealth_cf_proxy"}:
        proxy_rotator = get_scrape_proxy_rotator()
        proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        solve_cloudflare = internal_mode == "stealth_cf_proxy"
        _logger.info(
            "scrape_fetch_stealth_config mode=%s proxy_enabled=%s retries=%s solve_cloudflare=%s url=%s",
            internal_mode,
            proxy_rotator is not None,
            get_proxy_retry_count(),
            solve_cloudflare,
            url,
        )
        stealth_kwargs = {
            **base_kwargs,
            "timeout": max(60000, browser_timeout_ms),
            "retries": 1 if not solve_cloudflare else get_proxy_retry_count(default=1),
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


def detect_auto_escalation_signals(page: Selector) -> set[str]:
    """Detect whether HTTP results indicate JS/protection escalation is needed."""
    signals: set[str] = set()

    status = getattr(page, "status", None)
    if isinstance(status, int) and status in PROTECTION_STATUS_CODES:
        signals.add("protection")

    page_text = str(page.get_all_text(separator=" ", strip=True)).lower()
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
