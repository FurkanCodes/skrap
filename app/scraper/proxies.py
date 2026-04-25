from __future__ import annotations

"""Proxy configuration helpers for Scrapling fetchers."""

from functools import lru_cache

from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator

from app.core.config import get_settings


@lru_cache
def get_scrape_proxy_rotator() -> ProxyRotator | None:
    """Return a shared, thread-safe proxy rotator when scraping proxies are configured."""
    proxy_urls = get_settings().scrape_proxy_urls
    if not proxy_urls:
        return None
    return ProxyRotator(list(proxy_urls))
