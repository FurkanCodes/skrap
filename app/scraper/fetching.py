from __future__ import annotations

"""Fetcher selection and auto-escalation heuristics."""

import logging

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
from scrapling.parser import Selector

from .constants import JAVASCRIPT_HINTS, PROTECTION_HINTS, PROTECTION_STATUS_CODES
from .proxies import get_proxy_retry_count, get_scrape_proxy_rotator

_logger = logging.getLogger("scraper_api.scraper")


def initial_auto_fetch_mode(url: str) -> str:
    """Choose the first transport for auto mode."""
    _logger.info("scrape_auto_initial_mode mode=http reason=sequential_probe url=%s", url)
    return "http"


def auto_remaining_modes(
    current_mode: str,
    *,
    signals: set[str] | None = None,
) -> list[str]:
    """Return untried auto-mode fallbacks in the best order for observed signals."""
    seen = {current_mode}
    ordered: list[str]
    if signals and "protection" in signals:
        ordered = ["stealth", "dynamic"]
    else:
        ordered = ["dynamic", "stealth"]
    return [mode for mode in ordered if mode not in seen]


def fetch_page_with_mode(url: str, timeout: int, mode: str) -> Selector:
    """Fetch a page using the requested fetch transport mode.

    - `http`: lightweight HTTP fetcher
    - `dynamic`: Playwright-backed browser fetcher
    - `stealth`: hardened browser fetcher for protected targets
    """
    _logger.info("scrape_fetch_start mode=%s url=%s timeout_seconds=%s", mode, url, timeout)
    if mode == "http":
        page = Fetcher.get(
            url,
            timeout=timeout,
            retries=2,
            retry_delay=1,
            stealthy_headers=True,
            follow_redirects="safe",
        )
        _logger.info("scrape_fetch_complete mode=http status=%s url=%s", getattr(page, "status", None), url)
        return page

    browser_timeout_ms = max(1000, timeout * 1000)
    base_kwargs = {
        "timeout": browser_timeout_ms,
        "retries": 2,
        "retry_delay": 1,
        "headless": True,
        "disable_resources": True,
    }

    if mode == "dynamic":
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
        _logger.info("scrape_fetch_complete mode=dynamic status=%s url=%s", getattr(page, "status", None), url)
        return page
    if mode == "stealth":
        proxy_rotator = get_scrape_proxy_rotator()
        proxy_kwargs = {"proxy_rotator": proxy_rotator} if proxy_rotator is not None else {}
        _logger.info(
            "scrape_fetch_stealth_config proxy_enabled=%s retries=%s url=%s",
            proxy_rotator is not None,
            get_proxy_retry_count(),
            url,
        )
        stealth_kwargs = {
            **base_kwargs,
            "timeout": max(60000, browser_timeout_ms),
            "retries": get_proxy_retry_count(),
            "disable_resources": False,
            "network_idle": True,
            "wait": 1500,
            "solve_cloudflare": True,
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
        _logger.info("scrape_fetch_complete mode=stealth status=%s url=%s", getattr(page, "status", None), url)
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
    if "protection" in signals:
        return ["stealth", "dynamic"]
    return ["dynamic", "stealth"]
