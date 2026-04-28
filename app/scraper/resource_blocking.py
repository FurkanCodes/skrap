from __future__ import annotations

"""Browser request-blocking defaults for dynamic scraper transports."""

BROWSER_BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        "adform.net",
        "adnxs.com",
        "adpartners.io",
        "adsrvr.org",
        "audrte.com",
        "bidswitch.net",
        "casalemedia.com",
        "cloudflareinsights.com",
        "contentexchange.me",
        "creativecdn.com",
        "crwdcntrl.net",
        "demdex.net",
        "doubleclick.net",
        "efilli.com",
        "enliven.com", 
        "gstatic.com",   
        "reimg-teknosa-cloud-prod.mncdn.com",
        "eyeota.net",
        "facebook.com",
        "flix360.com",
        "google-analytics.com",
        "googleadservices.com",
        "googlesyndication.com",
        "googletagmanager.com",
        "gowit.com",
        "id5-sync.com",
        "iesnare.com",
        "onaudience.com",
        "openx.net",
        "pinterest.com",
        "poltio.com",
        "rlcdn.com",
        "semasio.net",
        "tiktok.com",
        "tiktokw.us",
        "visx.net",
        "weborama.fr",
        "yandex.ru",
        "yieldmo.com",
    }
)


def browser_resource_blocking_kwargs() -> dict[str, object]:
    """Return Scrapling browser options that drop nonessential ad/tracker traffic."""
    return {
        "block_ads": True,
        "blocked_domains": set(BROWSER_BLOCKED_DOMAINS),
    }
