from __future__ import annotations

from urllib.parse import urlparse

from .adapters import AkakceAdapter, GenericAdapter, NeweggAdapter, SiteAdapter

SUPPORTED_SITES: dict[str, str] = {
    "generic": "Generic extractor (fallback for any site)",
    "akakce": "Akakce adapter",
    "newegg": "Newegg adapter",
}

SITE_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "akakce": ("akakce.com",),
    "newegg": ("newegg.com",),
}

ADAPTERS: dict[str, SiteAdapter] = {
    "generic": GenericAdapter(),
    "akakce": AkakceAdapter(),
    "newegg": NeweggAdapter(),
}


def list_supported_sites() -> dict[str, str]:
    return dict(SUPPORTED_SITES)


def detect_site_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    for site_key, suffixes in SITE_DOMAIN_SUFFIXES.items():
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
            return site_key
    return "generic"
