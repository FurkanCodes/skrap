from __future__ import annotations

"""Proxy configuration helpers for Scrapling fetchers."""

from functools import lru_cache
import logging
from urllib.parse import urlparse

import httpx
from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator

from app.core.config import get_settings

_logger = logging.getLogger("scraper_api.scraper")
_SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}


def _mask_proxy_url(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    auth = "***:***@" if parsed.username or parsed.password else ""
    return f"{parsed.scheme}://{auth}{host}{port}"


def _is_valid_proxy_url(proxy_url: str) -> bool:
    parsed = urlparse(proxy_url)
    return parsed.scheme in _SUPPORTED_PROXY_SCHEMES and bool(parsed.netloc)


def _parse_proxy_list(text: str) -> tuple[str, ...]:
    proxies: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        candidate = line.split()[0].strip()
        if not _is_valid_proxy_url(candidate) or candidate in seen:
            continue
        seen.add(candidate)
        proxies.append(candidate)
    return tuple(proxies)


def _load_proxy_list_url(url: str) -> tuple[str, ...]:
    try:
        response = httpx.get(url, timeout=10, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        _logger.warning("scrape_proxy_list_fetch_failed url=%s", url, exc_info=True)
        return ()
    proxies = _parse_proxy_list(response.text)
    _logger.info("scrape_proxy_list_loaded url=%s count=%s", url, len(proxies))
    return proxies


@lru_cache
def get_scrape_proxy_rotator() -> ProxyRotator | None:
    """Return a shared, thread-safe proxy rotator when scraping proxies are configured."""
    settings = get_settings()
    proxy_urls = list(settings.scrape_proxy_urls)
    for list_url in settings.scrape_proxy_list_urls:
        proxy_urls.extend(_load_proxy_list_url(list_url))
    proxy_urls = list(dict.fromkeys(proxy_urls))
    if not proxy_urls:
        return None
    _logger.info(
        "scrape_proxy_rotator_enabled count=%s proxies=%s",
        len(proxy_urls),
        ",".join(_mask_proxy_url(url) for url in proxy_urls),
    )
    return ProxyRotator(list(proxy_urls))
