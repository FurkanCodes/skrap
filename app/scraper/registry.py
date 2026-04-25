from __future__ import annotations

"""Site registry and domain-to-adapter routing."""

from urllib.parse import urlparse

from .adapters import (
    AkakceAdapter,
    ConfiguredSiteAdapter,
    GenericAdapter,
    N11Adapter,
    NeweggAdapter,
    SiteAdapter,
)

SUPPORTED_SITES: dict[str, str] = {
    "generic": "Generic extractor (fallback for any site)",
    "akakce": "Akakce adapter",
    "a101": "A101 adapter",
    "aliexpress": "AliExpress adapter",
    "amazon": "Amazon adapter",
    "bestbuy": "Best Buy adapter",
    "ciceksepeti": "CicekSepeti adapter",
    "costco": "Costco adapter",
    "ebay": "eBay adapter",
    "etsy": "Etsy adapter",
    "gurgencler": "Gurgencler adapter",
    "hepsiburada": "Hepsiburada adapter",
    "homedepot": "Home Depot adapter",
    "ikea": "IKEA adapter",
    "leroymerlin": "Leroy Merlin adapter",
    "lowes": "Lowe's adapter",
    "mediamarkt": "MediaMarkt adapter",
    "mediaworld": "MediaWorld adapter",
    "newegg": "Newegg adapter",
    "n11": "n11 adapter",
    "pttavm": "PttAVM adapter",
    "shein": "SHEIN adapter",
    "sinerji": "Sinerji adapter",
    "target": "Target adapter",
    "temu": "Temu adapter",
    "teknosa": "Teknosa adapter",
    "trendyol": "Trendyol adapter",
    "vatanbilgisayar": "Vatan Bilgisayar adapter",
    "vinted": "Vinted adapter",
    "zalando": "Zalando adapter",
}

SITE_DOMAIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "a101": ("a101.com.tr",),
    "akakce": ("akakce.com",),
    "aliexpress": ("aliexpress.com",),
    "amazon": (
        "amazon.com",
        "amazon.co.uk",
        "amazon.de",
        "amazon.fr",
        "amazon.es",
        "amazon.com.tr",
    ),
    "bestbuy": ("bestbuy.com",),
    "ciceksepeti": ("ciceksepeti.com",),
    "costco": ("costco.com",),
    "ebay": ("ebay.com", "ebay.co.uk", "ebay.de", "ebay.fr", "ebay.es"),
    "etsy": ("etsy.com",),
    "gurgencler": ("gurgencler.com.tr",),
    "hepsiburada": ("hepsiburada.com",),
    "homedepot": ("homedepot.com",),
    "ikea": ("ikea.com",),
    "leroymerlin": ("leroymerlin.fr", "leroymerlin.es", "leroymerlin.it"),
    "lowes": ("lowes.com",),
    "mediamarkt": ("mediamarkt.com", "mediamarkt.de", "mediamarkt.es", "mediamarkt.com.tr"),
    "mediaworld": ("mediaworld.it",),
    "newegg": ("newegg.com",),
    "n11": ("n11.com",),
    "pttavm": ("pttavm.com",),
    "shein": ("shein.com",),
    "sinerji": ("sinerji.gen.tr",),
    "target": ("target.com",),
    "temu": ("temu.com",),
    "teknosa": ("teknosa.com",),
    "trendyol": ("trendyol.com",),
    "vatanbilgisayar": ("vatanbilgisayar.com",),
    "vinted": ("vinted.fr", "vinted.it", "vinted.es"),
    "zalando": ("zalando.de", "zalando.it", "zalando.es"),
}

ADAPTERS: dict[str, SiteAdapter] = {
    "generic": GenericAdapter(),
    "a101": ConfiguredSiteAdapter(
        key="a101",
        price_selectors=(
            ".current-price::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
        default_currency="TRY",
    ),
    "akakce": AkakceAdapter(),
    "aliexpress": ConfiguredSiteAdapter(
        key="aliexpress",
        price_selectors=(
            "[class*='price--currentPrice']::text",
            ".product-price-current::text",
            "[itemprop='price']::attr(content)",
        ),
    ),
    "amazon": ConfiguredSiteAdapter(
        key="amazon",
        price_selectors=(
            "#corePrice_feature_div span.a-offscreen::text",
            "span#priceblock_ourprice::text",
            "span#priceblock_dealprice::text",
            "[data-a-color='price'] .a-offscreen::text",
        ),
    ),
    "bestbuy": ConfiguredSiteAdapter(
        key="bestbuy",
        price_selectors=(
            ".priceView-customer-price span::text",
            "[data-testid='customer-price']::text",
            "[itemprop='price']::attr(content)",
        ),
    ),
    "ciceksepeti": ConfiguredSiteAdapter(
        key="ciceksepeti",
        price_selectors=(
            ".js-product-price::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
        default_currency="TRY",
    ),
    "costco": ConfiguredSiteAdapter(
        key="costco",
        price_selectors=(
            ".product-price::text",
            "[automation-id='productPriceOutput']::text",
            "[itemprop='price']::attr(content)",
        ),
    ),
    "ebay": ConfiguredSiteAdapter(
        key="ebay",
        price_selectors=(
            "div.x-price-primary span::text",
            "span[itemprop='price']::attr(content)",
            "[data-testid='x-price-primary']::text",
        ),
    ),
    "etsy": ConfiguredSiteAdapter(
        key="etsy",
        price_selectors=(
            "[data-buy-box-region='price'] *::text",
            "[itemprop='price']::attr(content)",
            ".wt-text-title-03::text",
        ),
    ),
    "gurgencler": ConfiguredSiteAdapter(
        key="gurgencler",
        price_selectors=(
            "[itemprop='price']::attr(content)",
            "meta[property='product:price:amount']::attr(content)",
            "[class*='price']::text",
        ),
        default_currency="TRY",
    ),
    "hepsiburada": ConfiguredSiteAdapter(
        key="hepsiburada",
        price_selectors=(
            "[data-test-id='price-current-price']::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
        default_currency="TRY",
    ),
    "homedepot": ConfiguredSiteAdapter(
        key="homedepot",
        price_selectors=(
            "[data-testid='price-format-wrapper']::text",
            "[itemprop='price']::attr(content)",
            ".price-format__main-price span::text",
        ),
    ),
    "ikea": ConfiguredSiteAdapter(
        key="ikea",
        price_selectors=(
            "[data-testid='pip-price']::text",
            "[itemprop='price']::attr(content)",
            ".pip-price-module__current-price::text",
        ),
    ),
    "leroymerlin": ConfiguredSiteAdapter(
        key="leroymerlin",
        price_selectors=(
            "[data-qa-product-price]::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
    ),
    "lowes": ConfiguredSiteAdapter(
        key="lowes",
        price_selectors=(
            "[data-testid='main-price']::text",
            "[itemprop='price']::attr(content)",
            ".main-price::text",
        ),
    ),
    "mediamarkt": ConfiguredSiteAdapter(
        key="mediamarkt",
        price_selectors=(
            ".price__main::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
    ),
    "mediaworld": ConfiguredSiteAdapter(
        key="mediaworld",
        price_selectors=(
            ".price__main::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
    ),
    "newegg": NeweggAdapter(),
    "n11": N11Adapter(),
    "pttavm": ConfiguredSiteAdapter(
        key="pttavm",
        price_selectors=(
            ".newPrice::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
        default_currency="TRY",
    ),
    "shein": ConfiguredSiteAdapter(
        key="shein",
        price_selectors=(
            "[data-testid='final-price']::text",
            ".product-intro__head-price::text",
            "[itemprop='price']::attr(content)",
        ),
    ),
    "sinerji": ConfiguredSiteAdapter(
        key="sinerji",
        price_selectors=(
            "[itemprop='price']::attr(content)",
            "meta[property='product:price:amount']::attr(content)",
            "[class*='price']::text",
        ),
        default_currency="TRY",
    ),
    "target": ConfiguredSiteAdapter(
        key="target",
        price_selectors=(
            "[data-test='product-price']::text",
            "[itemprop='price']::attr(content)",
            ".styles_ndsPrice__H5s4d::text",
        ),
    ),
    "temu": ConfiguredSiteAdapter(
        key="temu",
        price_selectors=(
            "[class*='price']::text",
            "[itemprop='price']::attr(content)",
        ),
    ),
    "teknosa": ConfiguredSiteAdapter(
        key="teknosa",
        price_selectors=(
            "[itemprop='price']::attr(content)",
            "meta[property='product:price:amount']::attr(content)",
            "[data-testid*='price']::text",
            "[class*='price']::text",
            "[id*='price']::text",
        ),
        default_currency="TRY",
    ),
    "trendyol": ConfiguredSiteAdapter(
        key="trendyol",
        price_selectors=(
            ".prc-dsc::text",
            ".prc-slg::text",
            "[itemprop='price']::attr(content)",
        ),
        default_currency="TRY",
    ),
    "vatanbilgisayar": ConfiguredSiteAdapter(
        key="vatanbilgisayar",
        price_selectors=(
            ".product-list__price::text",
            "[itemprop='price']::attr(content)",
            ".price::text",
        ),
        default_currency="TRY",
    ),
    "vinted": ConfiguredSiteAdapter(
        key="vinted",
        price_selectors=(
            "[data-testid='item-price']::text",
            "[itemprop='price']::attr(content)",
            ".web_ui__Text__text::text",
        ),
    ),
    "zalando": ConfiguredSiteAdapter(
        key="zalando",
        price_selectors=(
            "[data-testid='price']::text",
            "[itemprop='price']::attr(content)",
            ".o5if6::text",
        ),
    ),
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
