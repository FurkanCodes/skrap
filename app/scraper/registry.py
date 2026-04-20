from __future__ import annotations

"""Site registry and domain-to-adapter routing."""

from urllib.parse import urlparse

from .adapters import AkakceAdapter, GenericAdapter, N11Adapter, NeweggAdapter, SiteAdapter

SUPPORTED_SITES: dict[str, str] = {
    "generic": "Generic extractor (fallback for any site)",
    "akakce": "Akakce adapter",
    "newegg": "Newegg adapter",
    "n11": "n11 adapter",
}

SITE_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "akakce": ("akakce.com",),
    "newegg": ("newegg.com",),
    "n11": ("n11.com",),
}

ADAPTERS: dict[str, SiteAdapter] = {
    "generic": GenericAdapter(),
    "akakce": AkakceAdapter(),
    "newegg": NeweggAdapter(),
    "n11": N11Adapter(),
}


def list_supported_sites() -> dict[str, str]:
    """Return a copy so callers cannot mutate registry state."""
    return dict(SUPPORTED_SITES)


def detect_site_key(url: str) -> str:
    """Map URL host to adapter key, falling back to `generic`."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    for site_key, suffixes in SITE_DOMAIN_SUFFIXES.items():
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
            return site_key
    return "generic"
