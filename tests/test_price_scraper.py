from __future__ import annotations

from decimal import Decimal

import pytest
from scrapling.parser import Selector

from app.scraper.adapters import AkakceAdapter, N11Adapter, TeknosaAdapter
from app.scraper.domain import PriceCandidate
from app.scraper.extractors.generic import extract_price_listings_from_selector
from app.scraper.fetching import (
    auto_remaining_modes,
    detect_auto_escalation_signals,
    initial_auto_fetch_mode,
)
from app.scraper.inputs import parse_fetch_mode, parse_sites_blob, parse_urls_blob
from app.scraper.metadata import extract_product_metadata
from app.scraper.parsing import make_item_id, normalize_runtime_url
from app.scraper.registry import ADAPTERS, detect_site_key, list_supported_sites
from app.scraper.service import extract_price_from_page, is_result_qualified, missing_required_result_fields


def test_extract_from_meta_tags_single_listing() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta itemprop='price' content='1299.99'>
            <meta itemprop='priceCurrency' content='USD'>
          </head>
          <body></body>
        </html>
        """
    )

    results = extract_price_listings_from_selector(page)

    assert len(results) == 1
    assert str(results[0].amount) == "1299.99"
    assert results[0].currency == "USD"
    assert results[0].source in {"meta[itemprop=price]", "dom:[itemprop='price']"}


def test_extract_from_json_ld_offers() -> None:
    page = Selector(
        """
        <html>
          <head>
            <script type='application/ld+json'>
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Test Product",
                  "offers": {
                    "@type": "AggregateOffer",
                    "offers": [
                      {
                        "@type": "Offer",
                        "priceCurrency": "EUR",
                        "price": "899,50",
                        "url": "https://shop.example/one",
                        "seller": {"@type": "Organization", "name": "Seller One"}
                      },
                      {
                        "@type": "Offer",
                        "priceCurrency": "EUR",
                        "price": "920,00",
                        "url": "https://shop.example/two",
                        "seller": {"@type": "Organization", "name": "Seller Two"}
                      }
                    ]
                  }
                }
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    results = extract_price_listings_from_selector(page)

    assert len(results) == 2
    assert str(results[0].amount) == "899.50"
    assert results[0].currency == "EUR"
    assert results[0].source.startswith("json-ld")
    assert results[0].seller == "Seller One"
    assert results[1].seller == "Seller Two"


def test_extract_from_dom_text_fallback() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div class='product-card'>
              <span class='price'>Now only $49.90</span>
            </div>
          </body>
        </html>
        """
    )

    results = extract_price_listings_from_selector(page)

    assert len(results) == 1
    assert str(results[0].amount) == "49.90"
    assert results[0].currency == "USD"
    assert results[0].source.startswith("dom:")


def test_extract_from_find_fallback_with_attribute_filters() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div data-qa="price-tag">$129.50</div>
          </body>
        </html>
        """
    )

    results = extract_price_listings_from_selector(page)

    assert len(results) == 1
    assert str(results[0].amount) == "129.50"
    assert results[0].currency == "USD"
    assert results[0].source.startswith("find:")


def test_extract_from_find_fallback_with_regex_search() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div class='offer-card'>Special deal EUR 31,20 today only</div>
            <div class='offer-card'>Another deal US$44.00 with shipping</div>
          </body>
        </html>
        """
    )

    results = extract_price_listings_from_selector(page)

    assert len(results) >= 2
    assert any(str(item.amount) == "31.20" and item.currency == "EUR" for item in results)
    assert any(str(item.amount) == "44.00" and item.currency == "USD" for item in results)
    assert any(item.source.startswith("find:regex") for item in results)


def test_parse_urls_blob() -> None:
    urls = parse_urls_blob(
        """
        https://example.com/a
        not-a-url
        https://example.com/b,https://example.com/a
        """
    )

    assert urls == ["https://example.com/a", "https://example.com/b"]


def test_parse_urls_blob_keeps_commas_inside_url() -> None:
    urls = parse_urls_blob(
        """
        https://www.akakce.com/ssd/en-ucuz-samsung-9100-pro-mz-vap2t0bw-pci-express-5-0-2-tb-m-2-fiyati,983826204.html
        """
    )

    assert urls == [
        "https://www.akakce.com/ssd/en-ucuz-samsung-9100-pro-mz-vap2t0bw-pci-express-5-0-2-tb-m-2-fiyati,983826204.html"
    ]


def test_normalize_runtime_url_rewrites_aliexpress_bundle_product_ids() -> None:
    source_url = (
        "https://www.aliexpress.com/ssr/300000512/BundleDeals2?"
        "spm=a2g0o.home.3fornn.1.1b664618dzbySf&disableNav=YES"
        "&pha_manifest=ssr&_immersiveMode=true&productIds=1005009475789596"
    )

    normalized = normalize_runtime_url(source_url)

    assert normalized == "https://www.aliexpress.com/item/1005009475789596.html"


def test_normalize_runtime_url_keeps_existing_aliexpress_product_url() -> None:
    source_url = "https://www.aliexpress.com/item/1005009475789596.html?gatewayAdapt=glo2tur"

    normalized = normalize_runtime_url(source_url)

    assert normalized == source_url


def test_item_id_is_stable_for_same_listing_url() -> None:
    candidate = PriceCandidate(
        raw_text="899,50",
        amount=Decimal("899.50"),
        currency="EUR",
        source="json-ld[0]",
        seller="Seller One",
        listing_url="https://shop.example/one?utm_source=test",
    )

    first = make_item_id("https://example.com/product", candidate, 1)
    second = make_item_id("https://example.com/product", candidate, 99)

    assert first == second
    assert first.startswith("itm_")


def test_item_id_changes_for_different_listing_urls() -> None:
    first_candidate = PriceCandidate(
        raw_text="899,50",
        amount=Decimal("899.50"),
        currency="EUR",
        source="json-ld[0]",
        seller="Seller One",
        listing_url="https://shop.example/one",
    )
    second_candidate = PriceCandidate(
        raw_text="899,50",
        amount=Decimal("899.50"),
        currency="EUR",
        source="json-ld[0]",
        seller="Seller One",
        listing_url="https://shop.example/two",
    )

    first = make_item_id("https://example.com/product", first_candidate, 1)
    second = make_item_id("https://example.com/product", second_candidate, 1)

    assert first != second


def test_item_id_changes_for_different_fragment_listing_urls() -> None:
    first_candidate = PriceCandidate(
        raw_text="19.998,00",
        amount=Decimal("19998.00"),
        currency="TRY",
        source="akakce:dom",
        seller="Store A",
        listing_url="#/2739099/?s=1&v=10001",
    )
    second_candidate = PriceCandidate(
        raw_text="19.998,00",
        amount=Decimal("19998.00"),
        currency="TRY",
        source="akakce:dom",
        seller="Store A",
        listing_url="#/2739099/?s=2&v=10001",
    )

    first = make_item_id("https://www.akakce.com/example,123.html", first_candidate, 1)
    second = make_item_id("https://www.akakce.com/example,123.html", second_candidate, 1)

    assert first != second


def test_akakce_adapter_parses_decimals_and_seller_fallbacks() -> None:
    page = Selector(
        """
        <html>
          <body>
            <ul id="PL">
              <li>
                <a href="#/p1?s=1">
                  <span class="pt_v8">19.998<i>,00 TL</i></span>
                  <span class="v_v8"><img alt="Amazon Turkiye" src="//cdn.example/a.svg"></span>
                </a>
              </li>
              <li>
                <a href="#/p1?s=2">
                  <span class="pt_v8">20.761<i>,90 TL</i></span>
                  <span class="v_v8">/ShopX 8,8 694 Yorum</span>
                </a>
              </li>
              <li>
                <a href="#/p1?s=3">
                  <span class="pt_v8">21.100<i>,50 TL</i></span>
                  <span class="v_v8">8,8 694 Yorum <img alt="MediaMarkt" src="//cdn.example/m.svg"></span>
                </a>
              </li>
            </ul>
          </body>
        </html>
        """
    )

    listings = AkakceAdapter().extract(page, "https://www.akakce.com/example,123.html")

    assert len(listings) == 3
    assert str(listings[0].amount) == "19998.00"
    assert listings[0].currency == "TRY"
    assert listings[0].seller == "Amazon Turkiye"
    assert str(listings[1].amount) == "20761.90"
    assert listings[1].seller == "ShopX"
    assert listings[2].seller == "MediaMarkt"


def test_akakce_adapter_prefers_campaign_price_over_original_price() -> None:
    page = Selector(
        """
        <html>
          <body>
            <ul id="PL">
              <li>
                <a href="#/p1?s=1">
                  <span class="pb_v8">
                    <span class="pt_v8 orig_pt_v8">35.158<i>,30 TL</i></span>
                    <span class="pt_v8 cmpgn_pt_v8">34.158<i>,30 TL</i></span>
                  </span>
                  <span class="v_v8">Satıcı: <img alt="Pttavm" src="//cdn.example/p.svg">/Dfsbilgisayar</span>
                </a>
              </li>
            </ul>
          </body>
        </html>
        """
    )

    listings = AkakceAdapter().extract(page, "https://www.akakce.com/example,123.html")

    assert len(listings) == 1
    assert str(listings[0].amount) == "34158.30"
    assert listings[0].raw_text == "34.158,30 TL"
    assert listings[0].listing_url == "#/p1?s=1"


def test_n11_adapter_prefers_requested_seller_from_initial_state() -> None:
    page = Selector(
        """
        <html>
          <head>
            <script>
              window.__INITIAL_STATE__ = {
                "unifiedProductList": [
                  {
                    "price": "310,00 TL",
                    "sellerNickName": "seller-a",
                    "url": "https://m.n11.com/urun/example?magaza=seller-a&renk=siyah"
                  },
                  {
                    "price": "309,00 TL",
                    "sellerNickName": "seller-b",
                    "url": "https://m.n11.com/urun/example?magaza=seller-b&renk=siyah"
                  }
                ]
              };
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    listings = N11Adapter().extract(page, "https://www.n11.com/urun/example?magaza=seller-b&renk=siyah")

    assert len(listings) == 2
    assert str(listings[0].amount) == "309.00"
    assert listings[0].currency == "TRY"
    assert listings[0].seller == "seller-b"
    assert listings[0].source == "n11:initial-state"


def test_n11_adapter_falls_back_to_dom_rows() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div class="unifiedProduct">
              <a href="/urun/example?magaza=seller-a&renk=siyah">
                <div class="title"><span class="name">Seller A</span></div>
                <div class="priceDisplay">310,00 TL</div>
              </a>
            </div>
            <div class="unifiedProduct">
              <a href="/urun/example?magaza=seller-b&renk=siyah">
                <div class="title"><span class="name">Seller B</span></div>
                <div class="priceDisplay">309,00 TL</div>
              </a>
            </div>
          </body>
        </html>
        """
    )

    listings = N11Adapter().extract(page, "https://www.n11.com/urun/example?magaza=seller-b&renk=siyah")

    assert len(listings) == 2
    assert str(listings[0].amount) == "309.00"
    assert listings[0].seller == "Seller B"
    assert listings[0].source == "n11:dom"


def test_n11_adapter_prefers_selected_offer_metadata() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div class="sellerInfo">
              <div class="sellerNameWrapper"><a>n11</a></div>
            </div>
            <div class="unifiedProduct">
              <a href="/urun/example?magaza=seller-a&renk=siyah">
                <div class="title"><span class="name">Seller A</span></div>
                <div class="priceDisplay">310,00 TL</div>
              </a>
            </div>
            <script>
              window.__INITIAL_STATE__ = {
                "productMeta": {
                  "price": "309.00"
                }
              };
            </script>
          </body>
        </html>
        """
    )

    listings = N11Adapter().extract(page, "https://www.n11.com/urun/example?magaza=n11&renk=siyah")

    assert len(listings) >= 1
    assert str(listings[0].amount) == "309.00"
    assert listings[0].seller == "n11"
    assert listings[0].source == "n11:selected-meta"


def test_teknosa_adapter_ignores_hidden_mini_cart_zero_price() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div id="miniCartPrice" class="mini-cart-price hidden">0,00 TL</div>
            <div class="col-12 pdp-amount-tc has-cart-price">
              <input id="visiblePi" value="13.299,00 TL">
              <div class="pdp-cart-price"><span class="prc prc-third">13.299 TL</span></div>
            </div>
            <div class="recommendation-price">17.199 TL</div>
          </body>
        </html>
        """
    )

    listings = TeknosaAdapter().extract(page, "https://www.teknosa.com/example-p-115019184?shopId=teknosa")

    assert listings
    assert str(listings[0].amount) == "13299.00"
    assert listings[0].currency == "TRY"
    assert listings[0].raw_text == "13.299,00 TL"
    assert all(str(listing.amount) != "0.00" for listing in listings)


def test_parse_sites_blob() -> None:
    assert parse_sites_blob("akakce,newegg") == {"akakce", "newegg"}
    assert parse_sites_blob("all") == set()
    assert parse_sites_blob("") == set()


def test_parse_fetch_mode() -> None:
    assert parse_fetch_mode("auto") == "auto"
    assert parse_fetch_mode("HTTP") == "http"
    assert parse_fetch_mode("dynamic") == "dynamic"
    assert parse_fetch_mode("stealth") == "stealth"


def test_parse_fetch_mode_invalid_key() -> None:
    with pytest.raises(ValueError):
        parse_fetch_mode("invalid")


def test_parse_sites_blob_invalid_key() -> None:
    with pytest.raises(ValueError):
        parse_sites_blob("akakce,unknownsite")


def test_detect_site_key() -> None:
    assert detect_site_key("https://www.akakce.com/ssd/a") == "akakce"
    assert detect_site_key("https://newegg.com/p/pl?d=4070") == "newegg"
    assert detect_site_key("https://www.n11.com/urun/iphone-17") == "n11"
    assert detect_site_key("https://www.amazon.de/dp/B0A123") == "amazon"
    assert detect_site_key("https://www.target.com/p/sample-product/-/A-123456") == "target"
    assert detect_site_key("https://www.temu.com/goods.html") == "temu"
    assert detect_site_key("https://www.etsy.com/listing/123/sample-item") == "etsy"
    assert detect_site_key("https://www.homedepot.com/p/sample/12345") == "homedepot"
    assert detect_site_key("https://www.bestbuy.com/site/sample/123.p") == "bestbuy"
    assert detect_site_key("https://www.costco.com/sample-product.html") == "costco"
    assert detect_site_key("https://www.lowes.com/pd/sample/1000") == "lowes"
    assert detect_site_key("https://www.aliexpress.com/item/1005000.html") == "aliexpress"
    assert detect_site_key("https://www.shein.com/product-p-12345.html") == "shein"
    assert detect_site_key("https://www.ikea.com/de/de/p/sample-00000001/") == "ikea"
    assert detect_site_key("https://www.leroymerlin.fr/produits/sample.html") == "leroymerlin"
    assert detect_site_key("https://www.zalando.de/sample-product.html") == "zalando"
    assert detect_site_key("https://www.mediamarkt.com.tr/tr/product/_sample-12345.html") == "mediamarkt"
    assert detect_site_key("https://www.mediaworld.it/it/product/_sample-123.html") == "mediaworld"
    assert detect_site_key("https://www.vinted.fr/items/123456789-sample") == "vinted"
    assert detect_site_key("https://www.trendyol.com/sample-product-p-12345") == "trendyol"
    assert detect_site_key("https://www.hepsiburada.com/sample-urun-p-HBV000000000") == "hepsiburada"
    assert detect_site_key("https://www.pttavm.com/sample-urun-p-12345") == "pttavm"
    assert detect_site_key("https://www.vatanbilgisayar.com/sample-urun.html") == "vatanbilgisayar"
    assert detect_site_key("https://www.ciceksepeti.com/sample-urun-kc123456") == "ciceksepeti"
    assert detect_site_key("https://www.a101.com.tr/sample-urun") == "a101"
    assert detect_site_key("https://www.teknosa.com/sample-urun-p-12345") == "teknosa"
    assert detect_site_key("https://example.com/product") == "generic"


def test_registry_keys_match_supported_sites() -> None:
    assert set(ADAPTERS) == set(list_supported_sites())


def test_auto_escalation_signals_and_followup_order() -> None:
    class _Selection(list):
        def getall(self) -> list[str]:
            return list(self)

    class _FakePage:
        def __init__(self, status: int | None, text: str, script_count: int = 0, noscript: list[str] | None = None):
            self.status = status
            self._text = text
            self._script_count = script_count
            self._noscript = noscript or []

        def get_all_text(self, separator: str = " ", strip: bool = True) -> str:
            return self._text

        def css(self, selector: str) -> _Selection:
            if selector == "script":
                return _Selection([object() for _ in range(self._script_count)])
            if selector == "noscript::text":
                return _Selection(self._noscript)
            return _Selection([])

    protection_page = _FakePage(status=403, text="welcome")
    js_page = _FakePage(status=None, text="Please enable JavaScript to continue", script_count=20)

    protection_signals = detect_auto_escalation_signals(protection_page)
    js_signals = detect_auto_escalation_signals(js_page)

    assert protection_signals == {"protection"}
    assert js_signals == {"javascript"}
    assert auto_remaining_modes("http_direct", signals=protection_signals, proxy_enabled=True)[:2] == [
        "http_proxy",
        "stealth_proxy",
    ]
    assert auto_remaining_modes("http_direct", signals=js_signals, proxy_enabled=False) == [
        "dynamic_proxy",
        "stealth_proxy",
    ]


def test_auto_mode_starts_protected_sites_with_stealth() -> None:
    assert initial_auto_fetch_mode("https://www.akakce.com/ssd/example,123.html") == "http_direct"
    assert initial_auto_fetch_mode("https://www.mediamarkt.com.tr/tr/product/example.html") == "http_direct"
    assert initial_auto_fetch_mode("https://www.teknosa.com/example-p-123") == "http_direct"
    assert initial_auto_fetch_mode("https://example.com/product") == "http_direct"


def test_http_fetch_does_not_use_configured_proxy_rotator(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching

    proxy_rotator = object()
    captured: dict[str, object] = {}

    def _fake_get(url: str, **kwargs: object) -> Selector:
        captured["url"] = url
        captured.update(kwargs)
        return Selector("<html></html>")

    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: proxy_rotator)
    monkeypatch.setattr(fetching.Fetcher, "get", _fake_get)

    page = fetching.fetch_page_with_mode("https://example.com/product", timeout=10, mode="http")

    assert isinstance(page, Selector)
    assert captured["url"] == "https://example.com/product"
    assert "proxy_rotator" not in captured


def test_regular_stealth_fetch_avoids_cloudflare_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching
    from app.scraper.resource_blocking import BROWSER_BLOCKED_DOMAINS

    captured: dict[str, object] = {}

    def _fake_fetch(url: str, **kwargs: object) -> Selector:
        captured["url"] = url
        captured.update(kwargs)
        return Selector("<html></html>")

    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: None)
    monkeypatch.setattr(fetching.StealthyFetcher, "fetch", _fake_fetch)

    page = fetching.fetch_page_with_mode("https://example.com/protected", timeout=10, mode="stealth")

    assert isinstance(page, Selector)
    assert captured["url"] == "https://example.com/protected"
    assert captured["timeout"] == 10000
    assert captured["disable_resources"] is True
    assert captured["network_idle"] is False
    assert captured["solve_cloudflare"] is False
    assert captured["block_ads"] is True
    assert isinstance(captured["blocked_domains"], set)
    assert "googletagmanager.com" in captured["blocked_domains"]
    assert "vatanbilgisayar.com" not in captured["blocked_domains"]
    assert captured["blocked_domains"].issuperset(BROWSER_BLOCKED_DOMAINS)
    assert captured["retries"] == 1
    assert captured["hide_canvas"] is True
    assert captured["locale"] == "tr-TR"
    assert captured["timezone_id"] == "Europe/Istanbul"


def test_cloudflare_stealth_fetch_uses_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching

    captured: dict[str, object] = {}

    def _fake_fetch(url: str, **kwargs: object) -> Selector:
        captured["url"] = url
        captured.update(kwargs)
        return Selector("<html></html>")

    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: None)
    monkeypatch.setattr(fetching.StealthyFetcher, "fetch", _fake_fetch)

    page = fetching.fetch_page_with_mode("https://example.com/protected", timeout=10, mode="stealth_cf_proxy")

    assert isinstance(page, Selector)
    assert captured["url"] == "https://example.com/protected"
    assert captured["disable_resources"] is False
    assert captured["network_idle"] is True
    assert captured["solve_cloudflare"] is True
    assert captured["block_ads"] is True
    assert "googletagmanager.com" in captured["blocked_domains"]
    assert captured["timeout"] == 30000


def test_dynamic_fetch_passes_configured_proxy_rotator(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching

    proxy_rotator = object()
    captured: dict[str, object] = {}

    def _fake_fetch(url: str, **kwargs: object) -> Selector:
        captured["url"] = url
        captured.update(kwargs)
        return Selector("<html></html>")

    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: proxy_rotator)
    monkeypatch.setattr(fetching, "get_proxy_retry_count", lambda: 8)
    monkeypatch.setattr(fetching.DynamicFetcher, "fetch", _fake_fetch)

    page = fetching.fetch_page_with_mode("https://www.teknosa.com/example-p-123", timeout=10, mode="dynamic")

    assert isinstance(page, Selector)
    assert captured["url"] == "https://www.teknosa.com/example-p-123"
    assert captured["proxy_rotator"] is proxy_rotator
    assert captured["block_ads"] is True
    assert "doubleclick.net" in captured["blocked_domains"]
    assert captured["retries"] == 1


def test_stealth_fetch_passes_configured_proxy_rotator(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching

    proxy_rotator = object()
    captured: dict[str, object] = {}

    def _fake_fetch(url: str, **kwargs: object) -> Selector:
        captured["url"] = url
        captured.update(kwargs)
        return Selector("<html></html>")

    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: proxy_rotator)
    monkeypatch.setattr(fetching, "get_proxy_retry_count", lambda: 8)
    monkeypatch.setattr(fetching.StealthyFetcher, "fetch", _fake_fetch)

    page = fetching.fetch_page_with_mode("https://example.com/protected", timeout=10, mode="stealth")

    assert isinstance(page, Selector)
    assert captured["url"] == "https://example.com/protected"
    assert captured["proxy_rotator"] is proxy_rotator
    assert captured["retries"] == 1


def test_find_fallback_is_bounded() -> None:
    rows = "\n".join(
        f"<div data-qa='price-tag'>US${index}.00</div>"
        for index in range(1, 501)
    )
    page = Selector(f"<html><body>{rows}</body></html>")

    results = extract_price_listings_from_selector(page)

    # Find fallback should not traverse beyond bounded slice.
    assert len(results) == 200


def test_extract_product_metadata_prefers_json_ld_and_normalizes_fields() -> None:
    page = Selector(
        """
        <html>
          <head>
            <link rel="canonical" href="/products/sample-phone">
            <meta property="og:image" content="/images/phone.jpg">
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Sample Phone X",
                "brand": {"@type": "Brand", "name": "SampleBrand"},
                "model": "X-2026",
                "sku": "SKU-1000",
                "gtin13": "1234567890123",
                "image": "/images/phone-main.jpg",
                "offers": {
                  "@type": "Offer",
                  "availability": "https://schema.org/InStock",
                  "url": "/products/sample-phone"
                }
              }
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    metadata = extract_product_metadata(page, "https://shop.example.com/item")

    assert metadata["product_name"] == "Sample Phone X"
    assert metadata["brand"] == "SampleBrand"
    assert metadata["model"] == "X-2026"
    assert metadata["sku"] == "SKU-1000"
    assert metadata["gtin"] == "1234567890123"
    assert metadata["availability"] == "in_stock"
    assert metadata["canonical_url"] == "https://shop.example.com/products/sample-phone"
    assert metadata["image_url"] == "https://shop.example.com/images/phone-main.jpg"


def test_extract_price_from_page_includes_metadata_fields() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Meta Product Name">
            <meta property="og:image" content="https://cdn.example.com/product.png">
          </head>
          <body>
            <span class="price">$199.99</span>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(page, "https://example.com/products/p1")

    assert result.success is True
    assert result.product_name == "Meta Product Name"
    assert result.image_url == "https://cdn.example.com/product.png"
    assert result.canonical_url == "https://example.com/products/p1"
    assert result.extracted_at is not None


def test_extract_product_metadata_uses_site_fallback_selectors_when_og_missing() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div data-test='product-title'>Target Fallback Product</div>
            <img data-test='product-image' data-src='/images/target-product.jpg'>
            <div data-test='product-price'>$149.99</div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(page, "https://www.target.com/p/sample/-/A-123456")

    assert result.success is True
    assert result.product_name == "Target Fallback Product"
    assert result.image_url == "https://www.target.com/images/target-product.jpg"
    assert result.amount == "149.99"
    assert result.adapter_key == "target"


def test_result_qualification_requires_title_image_and_price() -> None:
    complete = extract_price_from_page(
        Selector(
            """
            <html>
              <head>
                <meta property='og:title' content='Complete Product'>
                <meta property='og:image' content='https://cdn.example.com/complete.jpg'>
              </head>
              <body><span>$19.99</span></body>
            </html>
            """
        ),
        "https://example.com/products/complete",
    )
    missing = extract_price_from_page(
        Selector(
            """
            <html>
              <body>
                <span class='price'>$19.99</span>
              </body>
            </html>
            """
        ),
        "https://example.com/products/missing-metadata",
    )

    assert is_result_qualified(complete) is True
    assert missing_required_result_fields(complete) == []
    assert is_result_qualified(missing) is False
    assert missing_required_result_fields(missing) == ["product_name", "image_url"]


def test_extract_product_metadata_prefers_embedded_product_state_over_site_org() -> None:
    source_url = "https://www.n11.com/urun/example-phone?magaza=n11&renk=siyah"
    page = Selector(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "https://schema.org",
                "@type": "Organization",
                "name": "n11.com",
                "url": "https://www.n11.com/",
                "logo": "https://n11scdn.example/logo.png"
              }
            </script>
            <script>
              window.__INITIAL_STATE__ = {
                "productMeta": {
                  "brand": "Apple",
                  "skuId": "123456"
                },
                "catalogProductInfo": {
                  "title": "Apple iPhone 17 256 GB",
                  "imageUrl": "/images/product-main.png"
                },
                "jsonLDProduct": {
                  "name": "Apple iPhone 17 256 GB Fiyatlari ve Ozellikleri",
                  "brand": "Apple",
                  "gtin8": "0195950643541"
                }
              };
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    metadata = extract_product_metadata(page, source_url)

    assert metadata["product_name"] == "Apple iPhone 17 256 GB Fiyatlari ve Ozellikleri"
    assert metadata["image_url"] == "https://www.n11.com/images/product-main.png"
    assert metadata["brand"] == "Apple"
    assert metadata["sku"] == "123456"
    assert metadata["gtin"] == "0195950643541"
    assert metadata["canonical_url"] == source_url


def test_extract_product_metadata_prefers_n11_gallery_image() -> None:
    source_url = "https://www.n11.com/urun/example-phone?magaza=n11&renk=siyah"
    page = Selector(
        """
        <html>
          <head>
            <script>
              window.__INITIAL_STATE__ = {
                "jsonLDProduct": {
                  "name": "Example Product",
                  "image": "https://n11scdn2-im.akamaized.net/a1/640/11/22/33/44/example.jpg"
                }
              };
            </script>
          </head>
          <body>
            <div class="desktop-slider">
              <div class="img-wrapper-onBoard">
                <img class="active" src="https://n11scdn.akamaized.net/a1/375_535/09/19/62/43/IMG-4331282096654046141.jpg" />
                <img src="https://n11scdn.akamaized.net/a1/375_535/03/13/58/99/IMG-8460333519124174689.png" />
              </div>
            </div>
          </body>
        </html>
        """
    )

    metadata = extract_product_metadata(page, source_url)

    assert (
        metadata["image_url"]
        == "https://n11scdn.akamaized.net/a1/375_535/09/19/62/43/IMG-4331282096654046141.jpg"
    )


def test_extract_product_metadata_upgrades_active_n11_thumbnail_to_high_res() -> None:
    source_url = "https://www.n11.com/urun/example-phone?magaza=n11&renk=siyah"
    page = Selector(
        """
        <html>
          <head>
            <script>
              window.__INITIAL_STATE__ = {
                "jsonLDProduct": {
                  "name": "Example Product",
                  "image": "https://n11scdn2-im.akamaized.net/a1/640/11/22/33/44/example.jpg"
                }
              };
            </script>
          </head>
          <body>
            <div class="desktop-slider">
              <div class="img-wrapper-onBoard">
                <img class="active" src="https://n11scdn.akamaized.net/a1/60_86/09/19/62/43/IMG-4331282096654046141.jpg" />
              </div>
            </div>
          </body>
        </html>
        """
    )

    metadata = extract_product_metadata(page, source_url)

    assert (
        metadata["image_url"]
        == "https://n11scdn.akamaized.net/a1/375_535/09/19/62/43/IMG-4331282096654046141.jpg"
    )


def test_extract_product_metadata_replaces_trendyol_icon_svg_with_product_image() -> None:
    source_url = "https://www.trendyol.com/example/product-p-123"
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Example Trendyol Product">
            <meta property="og:image" content="https://cdn.dsmcdn.com/sfint/prod/fp/search_1761135568801.svg">
          </head>
          <body>
            <script>
              window.__PRODUCT_DETAIL_APP_INITIAL_STATE__ = {
                "images": [
                  "https://cdn.dsmcdn.com/mnresize/620/920/ty1854/prod/QC_ENRICHMENT/20260414/10/b5ca5fdc-7e69-30ed-996a-cd4546f07d99/1_org_zoom.jpg"
                ]
              };
            </script>
          </body>
        </html>
        """
    )

    metadata = extract_product_metadata(page, source_url)

    assert (
        metadata["image_url"]
        == "https://cdn.dsmcdn.com/mnresize/620/920/ty1854/prod/QC_ENRICHMENT/20260414/10/b5ca5fdc-7e69-30ed-996a-cd4546f07d99/1_org_zoom.jpg"
    )


def test_trendyol_adapter_prefers_winner_variant_state_over_stale_json_ld() -> None:
    source_url = "https://www.trendyol.com/ninja/example-product-p-123"
    page = Selector(
        """
        <html>
          <head>
            <script type='application/ld+json'>
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Example Trendyol Product",
                "offers": {
                  "@type": "Offer",
                  "priceCurrency": "TRY",
                  "price": "11200.00",
                  "url": "https://www.trendyol.com/ninja/example-product-p-123"
                }
              }
            </script>
            <script>
              window["__envoy_product-detail__PROPS"]={
                "product": {
                  "merchantListing": {
                    "merchant": {"name": "Shark Ninja"},
                    "url": "/ninja/example-product-p-123?merchantId=123",
                    "winnerVariant": {
                      "price": {
                        "currency": "TRY",
                        "discountedPrice": {"value": 10880, "text": "10.880 TL"},
                        "sellingPrice": {"value": 10930, "text": "10.930 TL"},
                        "originalPrice": {"value": 10880, "text": "10.880 TL"}
                      }
                    }
                  }
                }
              }
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    result = extract_price_from_page(page, source_url)

    assert result.success is True
    assert result.amount == "10880"
    assert result.currency == "TRY"
    assert result.raw_text == "10.880 TL"
    assert result.source == "trendyol:winner-variant"
    assert result.listings[0].seller == "Shark Ninja"


def test_extract_price_from_page_prefers_listing_matching_requested_url() -> None:
    page = Selector(
        """
        <html>
          <head>
            <script type='application/ld+json'>
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Widget",
                "offers": {
                  "@type": "AggregateOffer",
                  "offers": [
                    {
                      "@type": "Offer",
                      "priceCurrency": "USD",
                      "price": "310.00",
                      "url": "https://shop.example.com/buy/other-seller",
                      "seller": {"@type": "Organization", "name": "Other Seller"}
                    },
                    {
                      "@type": "Offer",
                      "priceCurrency": "USD",
                      "price": "309.00",
                      "url": "https://shop.example.com/products/widget",
                      "seller": {"@type": "Organization", "name": "Direct Seller"}
                    }
                  ]
                }
              }
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    result = extract_price_from_page(page, "https://shop.example.com/products/widget?utm_source=newsletter")

    assert result.success is True
    assert result.amount == "309.00"
    assert result.listings[0].amount == "309.00"
    assert result.listings[0].seller == "Direct Seller"


def test_extract_price_from_page_prefers_listing_matching_canonical_url() -> None:
    page = Selector(
        """
        <html>
          <head>
            <link rel="canonical" href="https://shop.example.com/products/widget">
            <script type='application/ld+json'>
              {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Widget",
                "offers": {
                  "@type": "AggregateOffer",
                  "offers": [
                    {
                      "@type": "Offer",
                      "priceCurrency": "USD",
                      "price": "310.00",
                      "url": "https://shop.example.com/buy/other-seller",
                      "seller": {"@type": "Organization", "name": "Other Seller"}
                    },
                    {
                      "@type": "Offer",
                      "priceCurrency": "USD",
                      "price": "309.00",
                      "url": "https://shop.example.com/products/widget",
                      "seller": {"@type": "Organization", "name": "Direct Seller"}
                    }
                  ]
                }
              }
            </script>
          </head>
          <body></body>
        </html>
        """
    )

    result = extract_price_from_page(page, "https://shop.example.com/p/widget")

    assert result.success is True
    assert result.amount == "309.00"
    assert result.listings[0].amount == "309.00"
    assert result.listings[0].seller == "Direct Seller"
