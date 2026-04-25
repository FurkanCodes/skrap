from __future__ import annotations

"""Proxy configuration helpers for Scrapling fetchers."""

from functools import lru_cache
import logging
from urllib.parse import urlparse

from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator

from app.core.config import get_settings

_logger = logging.getLogger("scraper_api.scraper")


def _mask_proxy_url(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    auth = "***:***@" if parsed.username or parsed.password else ""
    return f"{parsed.scheme}://{auth}{host}{port}"


@lru_cache
def get_scrape_proxy_rotator() -> ProxyRotator | None:
    """Return a shared, thread-safe proxy rotator when scraping proxies are configured."""
    proxy_urls = get_settings().scrape_proxy_urls
    if not proxy_urls:
        return None
    _logger.info(
        "scrape_proxy_rotator_enabled count=%s proxies=%s",
        len(proxy_urls),
        ",".join(_mask_proxy_url(url) for url in proxy_urls),
    )
    return ProxyRotator(list(proxy_urls))
