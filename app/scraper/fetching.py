from __future__ import annotations

"""Fetcher selection and auto-escalation heuristics."""

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher
from scrapling.parser import Selector

from .constants import JAVASCRIPT_HINTS, PROTECTION_HINTS, PROTECTION_STATUS_CODES


def fetch_page_with_mode(url: str, timeout: int, mode: str) -> Selector:
    """Fetch a page using the requested fetch transport mode.

    - `http`: lightweight HTTP fetcher
    - `dynamic`: Playwright-backed browser fetcher
    - `stealth`: hardened browser fetcher for protected targets
    """
    if mode == "http":
        return Fetcher.get(
            url,
            timeout=timeout,
            retries=2,
            retry_delay=1,
            stealthy_headers=True,
            follow_redirects="safe",
        )

    browser_timeout_ms = max(1000, timeout * 1000)
    base_kwargs = {
        "timeout": browser_timeout_ms,
        "retries": 2,
        "retry_delay": 1,
        "headless": True,
        "disable_resources": True,
    }

    if mode == "dynamic":
        return DynamicFetcher.fetch(url, **base_kwargs)
    if mode == "stealth":
        return StealthyFetcher.fetch(
            url,
            solve_cloudflare=True,
            block_webrtc=True,
            **base_kwargs,
        )

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
    if "javascript" in signals:
        return ["dynamic", "stealth"]
    return []
