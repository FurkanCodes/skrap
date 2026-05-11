from __future__ import annotations

from decimal import Decimal

import pytest
from scrapling.parser import Selector

from app.scraper.adapters import AkakceAdapter, HepsiburadaAdapter, N11Adapter, TeknosaAdapter
from app.scraper.domain import PriceCandidate
from app.scraper.extractors.generic import extract_price_listings_from_selector
from app.scraper.fetching import (
    auto_remaining_modes,
    detect_auto_escalation_signals,
    effective_fetch_timeout_seconds,
    initial_auto_fetch_mode,
)
from app.scraper.inputs import parse_fetch_mode, parse_sites_blob, parse_urls_blob
from app.scraper.metadata import extract_product_metadata
from app.scraper.parsing import (
    make_item_id,
    normalize_akakce_listing_url,
    normalize_runtime_url,
)
from app.scraper.registry import ADAPTERS, detect_site_key, list_supported_sites
from app.scraper.scrape_spider import MODE_TO_SESSION_ID, _balanced_spider_remaining_modes, _session_id_for_mode
from app.scraper.service import extract_price_from_page, fetch_price, is_result_qualified, missing_required_result_fields


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


def test_extract_price_from_page_marks_generic_price_without_identity_low_confidence() -> None:
    result = extract_price_from_page(
        Selector(
            """
            <html>
              <body>
                <span class='price'>Now only $49.90</span>
              </body>
            </html>
            """
        ),
        "https://example.com/products/anonymous-price",
    )

    assert result.success is True
    assert result.amount == "49.90"
    assert result.confidence == "low"
    assert "generic_dom" in result.signals
    assert "missing_product_identity" in result.signals


def test_extract_price_from_page_promotes_semantic_price_with_identity_to_high_confidence() -> None:
    result = extract_price_from_page(
        Selector(
            """
            <html>
              <head>
                <meta property='og:title' content='Known Product'>
                <meta property='og:image' content='https://cdn.example.com/known.jpg'>
                <meta itemprop='priceCurrency' content='USD'>
              </head>
              <body>
                <span itemprop='price'>99.99</span>
              </body>
            </html>
            """
        ),
        "https://example.com/products/known-product",
    )

    assert result.success is True
    assert result.amount == "99.99"
    assert result.confidence == "high"
    assert "semantic_price_attr" in result.signals
    assert "product_identity" in result.signals
    assert "metadata_confirmed" in result.signals


def test_extract_price_from_page_keeps_find_regex_fallback_low_confidence() -> None:
    result = extract_price_from_page(
        Selector(
            """
            <html>
              <body>Special deal $12.34 today only</body>
            </html>
            """
        ),
        "https://example.com/products/text-only-price",
    )

    assert result.success is True
    assert result.amount == "12.34"
    assert result.confidence == "low"
    assert "find_regex" in result.signals


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


def test_akakce_item_id_ignores_volatile_offer_fragment_prefix_and_rank() -> None:
    first_candidate = PriceCandidate(
        raw_text="20.750,00 TL",
        amount=Decimal("20750.00"),
        currency="TRY",
        source="akakce:dom",
        seller="Ucuzbudur.com",
        listing_url=(
            "#/8860528/?s=1&v=1538&p=983826204&c=15598&g=1472419210"
            "&f=%2fr%2f%3fpr%3d983826204%26vd%3d1538%26pg%3d1472419210"
        ),
    )
    second_candidate = PriceCandidate(
        raw_text="20.750,00 TL",
        amount=Decimal("20750.00"),
        currency="TRY",
        source="akakce:dom",
        seller="Ucuzbudur.com",
        listing_url=(
            "#/753723/?s=4&v=1538&p=983826204&c=15598&g=1472419210"
            "&f=%2fr%2f%3fpr%3d983826204%26vd%3d1538%26pg%3d1472419210"
        ),
    )
    base_url = (
        "https://www.akakce.com/ssd/en-ucuz-samsung-9100-pro-mz-vap2t0bw-"
        "pci-express-5-0-2-tb-m-2-fiyati,983826204.html"
    )

    first = make_item_id(base_url, first_candidate, 1)
    second = make_item_id(base_url, second_candidate, 99)

    assert first == second
    assert normalize_akakce_listing_url(base_url, first_candidate.listing_url) == (
        f"{base_url}#/offer?p=983826204&v=1538&c=15598&g=1472419210"
    )


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


def test_akakce_adapter_uses_current_price_when_coupon_and_cargo_text_are_nearby() -> None:
    page = Selector(
        """
        <html>
          <body>
            <ul id="PL">
              <li>
                <a href="#/8497867/?z=146&s=2">
                  <span class="pb_v8">
                    <span class="pt_v8 orig_pt_v8">34.605<i>,93 TL</i></span>
                    <span class="pt_v8 cmpgn_pt_v8">33.605<i>,93 TL</i></span>
                  </span>
                  <span>Ücretsiz kargo</span>
                  <span>Sepette 1.000 TL indirim PTTAVM1000</span>
                  <span class="v_v8">Satıcı: Pttavm/Dfsbilgisayar</span>
                </a>
              </li>
            </ul>
          </body>
        </html>
        """
    )

    listings = AkakceAdapter().extract(page, "https://www.akakce.com/example,123.html")

    assert len(listings) == 1
    assert str(listings[0].amount) == "33605.93"
    assert listings[0].raw_text == "33.605,93 TL"
    assert listings[0].seller == "Pttavm/Dfsbilgisayar"


def test_akakce_adapter_handles_changed_price_classes_and_missing_seller() -> None:
    page = Selector(
        """
        <html>
          <body>
            <ul id="PL">
              <li>
                <a href="#/p1?s=1">
                  <span class="offer-price-original">36.995 TL</span>
                  <span class="offer-price-current">33.606 TL</span>
                  <span>Yarın kargoda</span>
                </a>
              </li>
            </ul>
          </body>
        </html>
        """
    )

    listings = AkakceAdapter().extract(page, "https://www.akakce.com/example,123.html")

    assert len(listings) == 1
    assert str(listings[0].amount) == "33606"
    assert listings[0].raw_text == "33.606 TL"
    assert listings[0].seller is None


def test_akakce_adapter_falls_back_when_offer_list_is_missing() -> None:
    page = Selector(
        """
        <html>
          <body>
            <h1>AMD Ryzen 9 9950X3D</h1>
            <div class="product-price">33.605,93 TL</div>
          </body>
        </html>
        """
    )

    listings = AkakceAdapter().extract(page, "https://www.akakce.com/example,123.html")

    assert len(listings) == 1
    assert str(listings[0].amount) == "33605.93"
    assert listings[0].currency == "TRY"


def test_extract_price_from_page_keeps_akakce_dom_order_as_primary_listing() -> None:
    source_url = "https://www.akakce.com/example,123.html"
    page = Selector(
        f"""
        <html>
          <head>
            <link rel="canonical" href="{source_url}">
          </head>
          <body>
            <ul id="PL">
              <li>
                <a href="#/p1?s=1">
                  <span class="pt_v8">19.998<i>,00 TL</i></span>
                  <span class="v_v8">/First Seller</span>
                </a>
              </li>
              <li>
                <a href="{source_url}">
                  <span class="pt_v8">20.761<i>,90 TL</i></span>
                  <span class="v_v8">/Later Seller</span>
                </a>
              </li>
            </ul>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(page, source_url)

    assert result.success is True
    assert result.amount == "19998.00"
    assert result.listings[0].amount == "19998.00"
    assert result.listings[0].seller == "First Seller"
    assert result.listings[1].amount == "20761.90"
    assert result.listings[1].seller == "Later Seller"


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


def test_hepsiburada_adapter_prefers_cart_special_price_and_flags_source() -> None:
    page = Selector(
        """
        <html>
          <body>
            <section class="price-box">
              <div>Sepete özel fiyat</div>
              <div>105,75 TL</div>
              <div data-test-id="price-current-price">124,41 TL</div>
              <button>Kazancımı gör</button>
              <div>18,66 TL</div>
            </section>
          </body>
        </html>
        """
    )

    listings = HepsiburadaAdapter().extract(page, "https://www.hepsiburada.com/example-p-HBC000")

    assert len(listings) == 1
    assert str(listings[0].amount) == "105.75"
    assert listings[0].currency == "TRY"
    assert listings[0].raw_text == "Sepete özel fiyat 105,75 TL"
    assert listings[0].source == "hepsiburada:cart-special"


def test_hepsiburada_adapter_falls_back_to_current_price_without_cart_special() -> None:
    page = Selector(
        """
        <html>
          <body>
            <div data-test-id="price-current-price">124,41 TL</div>
          </body>
        </html>
        """
    )

    listings = HepsiburadaAdapter().extract(page, "https://www.hepsiburada.com/example-p-HBC000")

    assert len(listings) == 1
    assert str(listings[0].amount) == "124.41"
    assert listings[0].currency == "TRY"
    assert listings[0].source == "hepsiburada:selector:0"


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
        def __init__(
            self,
            status: int | None,
            text: str,
            script_count: int = 0,
            noscript: list[str] | None = None,
            html_content: str = "",
        ):
            self.status = status
            self._text = text
            self._script_count = script_count
            self._noscript = noscript or []
            self.html_content = html_content

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
    amazon_captcha_page = _FakePage(
        status=200,
        text="Amazon.com.tr",
        html_content='<html><script>ue_sn = "opfcaptcha.amazon.com.tr";</script></html>',
    )
    mediamarkt_normal_page = _FakePage(
        status=200,
        text="MediaMarkt privacy copy mentions captcha vendors while showing a normal product page",
        html_content="<html>captcha Amazon privacy vendor references</html>",
    )
    amazon_generic_captcha_page = _FakePage(
        status=200,
        text="Amazon.com.tr",
        html_content="<html>captcha Amazon</html>",
    )

    protection_signals = detect_auto_escalation_signals(protection_page)
    js_signals = detect_auto_escalation_signals(js_page)
    amazon_captcha_signals = detect_auto_escalation_signals(amazon_captcha_page)
    mediamarkt_signals = detect_auto_escalation_signals(
        mediamarkt_normal_page,
        url="https://www.mediamarkt.com.tr/tr/product/_sample-12345.html",
    )
    amazon_generic_captcha_signals = detect_auto_escalation_signals(
        amazon_generic_captcha_page,
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert protection_signals == {"protection"}
    assert js_signals == {"javascript"}
    assert amazon_captcha_signals == {"amazon_captcha", "protection"}
    assert mediamarkt_signals == set()
    assert amazon_generic_captcha_signals == {"amazon_captcha", "protection"}
    assert auto_remaining_modes("http_direct", signals=protection_signals, proxy_enabled=True)[:2] == [
        "http_proxy",
        "stealth_proxy",
    ]
    assert auto_remaining_modes("http_direct", signals=js_signals, proxy_enabled=False) == [
        "dynamic_direct",
        "stealth_direct",
    ]


def test_amazon_auto_mode_uses_proxy_browsers_before_direct_browser_fallback() -> None:
    modes = auto_remaining_modes(
        "http_proxy",
        signals={"amazon_captcha", "protection"},
        proxy_enabled=True,
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert modes == ["dynamic_proxy", "dynamic_direct", "stealth_direct"]


def test_amazon_auto_mode_uses_direct_browser_fallback_after_direct_captcha() -> None:
    modes = auto_remaining_modes(
        "http_direct",
        signals={"amazon_captcha", "protection"},
        proxy_enabled=True,
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert modes == ["dynamic_direct", "stealth_direct"]


def test_amazon_auto_mode_stops_after_plain_direct_parser_miss() -> None:
    modes = auto_remaining_modes(
        "http_direct",
        signals=set(),
        proxy_enabled=True,
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert modes == []


def test_amazon_auto_mode_falls_back_to_http_direct_after_clean_proxy_attempt() -> None:
    modes = auto_remaining_modes(
        "http_proxy",
        signals=set(),
        proxy_enabled=True,
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert modes == ["http_direct"]


def test_amazon_auto_jumps_to_direct_browser_after_proxy_tunnel_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching
    from app.scraper import service

    attempted_modes: list[str] = []

    def _fake_fetch_page(url: str, timeout: int, mode: str) -> Selector:
        attempted_modes.append(mode)
        if mode == "http_proxy":
            raise RuntimeError("CONNECT tunnel failed, response 402")
        if mode == "http_direct":
            return Selector('<html><script>ue_sn = "opfcaptcha.amazon.com.tr";</script></html>')
        if mode == "dynamic_direct":
            return Selector(
                """
                <html>
                  <head>
                    <meta property="og:title" content="Ninja CREAMi 7-in-1 Dondurma Makinesi">
                    <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
                  </head>
                  <body>
                    <div id="corePrice_feature_div"><span class="a-offscreen">11.877,00TL</span></div>
                  </body>
                </html>
                """
            )
        raise AssertionError(f"unexpected mode: {mode}")

    monkeypatch.setattr(service, "fetch_page_with_mode", _fake_fetch_page)
    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: object())

    result = fetch_price(
        "https://www.amazon.com.tr/Ninja-CREAMi-7-in-1-Dondurma-Makinesi/dp/B0G26N7D9N?th=1",
        fetch_mode="auto",
    )

    assert result.success is True
    assert result.amount == "11877.00"
    assert attempted_modes == ["http_proxy", "http_direct", "dynamic_direct"]


def test_spider_mode_map_supports_direct_browser_fallbacks() -> None:
    assert MODE_TO_SESSION_ID["dynamic_direct"] == "dynamic_direct"
    assert MODE_TO_SESSION_ID["stealth_direct"] == "stealth_direct"


def test_spider_uses_fast_amazon_sessions_for_amazon_urls() -> None:
    assert _session_id_for_mode(
        "http_direct",
        "https://www.amazon.com.tr/example/dp/B000000000",
    ) == "amazon_http_direct"
    assert _session_id_for_mode(
        "dynamic_direct",
        "https://www.amazon.com.tr/example/dp/B000000000",
    ) == "amazon_dynamic_direct"
    assert _session_id_for_mode(
        "dynamic_direct",
        "https://example.com/product",
    ) == "dynamic_direct"


def test_spider_amazon_captcha_uses_one_proxy_then_direct_browser_fallbacks() -> None:
    modes = _balanced_spider_remaining_modes(
        "http_proxy",
        signals={"amazon_captcha", "protection"},
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert modes == ["dynamic_proxy", "dynamic_direct", "stealth_direct"]


def test_spider_amazon_direct_captcha_uses_direct_browser_fallbacks() -> None:
    modes = _balanced_spider_remaining_modes(
        "http_direct",
        signals={"amazon_captcha", "protection"},
        url="https://www.amazon.com.tr/example/dp/B000000000",
    )

    assert modes == ["dynamic_direct", "stealth_direct"]


def test_spider_amazon_proxy_followup_skips_remaining_proxy_modes() -> None:
    modes = _balanced_spider_remaining_modes(
        "dynamic_proxy",
        signals=set(),
        url="https://www.amazon.com.tr/example/dp/B000000000",
        remaining_modes=["stealth_proxy", "dynamic_direct", "stealth_direct"],
    )

    assert modes == ["dynamic_direct", "stealth_direct"]


def test_auto_mode_starts_protected_sites_with_stealth() -> None:
    assert initial_auto_fetch_mode("https://www.akakce.com/ssd/example,123.html") == "http_direct"
    assert initial_auto_fetch_mode("https://www.mediamarkt.com.tr/tr/product/example.html") == "http_direct"
    assert initial_auto_fetch_mode("https://www.teknosa.com/example-p-123") == "http_direct"
    assert initial_auto_fetch_mode("https://example.com/product") == "http_direct"


def test_amazon_auto_mode_starts_with_proxy_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching

    monkeypatch.setattr(fetching, "get_scrape_proxy_rotator", lambda: object())

    assert initial_auto_fetch_mode("https://www.amazon.com.tr/example/dp/B000000000") == "http_proxy"


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


def test_amazon_http_fetch_uses_short_timeout_and_single_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.scraper import fetching

    captured: dict[str, object] = {}

    def _fake_get(url: str, **kwargs: object) -> Selector:
        captured["url"] = url
        captured.update(kwargs)
        return Selector("<html></html>")

    monkeypatch.setattr(fetching.Fetcher, "get", _fake_get)

    page = fetching.fetch_page_with_mode(
        "https://www.amazon.com.tr/example/dp/B000000000",
        timeout=30,
        mode="http_direct",
    )

    assert isinstance(page, Selector)
    assert captured["timeout"] == effective_fetch_timeout_seconds(
        "https://www.amazon.com.tr/example/dp/B000000000",
        30,
        "http_direct",
    )
    assert captured["timeout"] == 8
    assert captured["retries"] == 1


def test_amazon_dynamic_proxy_fetch_caps_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
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

    page = fetching.fetch_page_with_mode(
        "https://www.amazon.com.tr/example/dp/B000000000",
        timeout=30,
        mode="dynamic_proxy",
    )

    assert isinstance(page, Selector)
    assert captured["timeout"] == 14000
    assert captured["retries"] == 1


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
    assert captured["retries"] == 8


def test_dynamic_direct_fetch_skips_configured_proxy_rotator(monkeypatch: pytest.MonkeyPatch) -> None:
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

    page = fetching.fetch_page_with_mode("https://www.amazon.com.tr/example/dp/B000000000", timeout=10, mode="dynamic_direct")

    assert isinstance(page, Selector)
    assert captured["url"] == "https://www.amazon.com.tr/example/dp/B000000000"
    assert "proxy_rotator" not in captured
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
    assert captured["retries"] == 8


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


def test_amazon_adapter_extracts_core_price_from_rendered_product_page() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Ninja CREAMi 7-in-1 Dondurma Makinesi">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="corePrice_feature_div">
              <span class="a-price">
                <span class="a-offscreen">11.877,00TL</span>
              </span>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.com.tr/Ninja-CREAMi-7-in-1-Dondurma-Makinesi/dp/B0G26N7D9N?th=1",
    )

    assert result.success is True
    assert result.amount == "11877.00"
    assert result.currency == "TRY"
    assert result.raw_text == "11.877,00TL"
    assert result.source.startswith("amazon:scoped-")


def test_amazon_adapter_prefers_main_price_over_installments_and_list_price() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Amazon Product">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="corePriceDisplay_desktop_feature_div">
              <span id="apex-pricetopay-accessibility-label" class="aok-offscreen">13.702,00 TL</span>
              <span class="a-price a-text-price">
                <span class="a-offscreen">15.999,00 TL</span>
              </span>
            </div>
            <div id="installmentCalculator_feature_div">
              <span class="a-price">
                <span class="a-offscreen">1.142,00 TL</span>
              </span>
              <span>12 taksit</span>
            </div>
            <div id="coupon_feature_div">
              <span>500,00 TL kupon</span>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.com.tr/dp/B0D6YXHYCX",
    )

    assert result.success is True
    assert result.amount == "13702.00"
    assert result.currency == "TRY"
    assert result.raw_text == "13.702,00 TL"
    assert result.source.startswith("amazon:scoped-exact:")


def test_amazon_adapter_rejects_installment_context_inside_buybox() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Amazon Product">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="desktop_buybox">
              <div id="installmentPaymentPrice_feature_div">
                <span class="a-price">
                  <span class="a-offscreen">1.142,00 TL</span>
                </span>
                <span>12 taksit</span>
              </div>
              <div class="apexPriceToPay">
                <span class="a-price">
                  <span class="a-offscreen">13.702,00 TL</span>
                </span>
              </div>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.com.tr/dp/B0D6YXHYCX",
    )

    assert result.success is True
    assert result.amount == "13702.00"
    assert result.raw_text == "13.702,00 TL"


def test_amazon_adapter_does_not_promote_lone_installment_price() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Amazon Product">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="desktop_buybox">
              <div id="installmentPaymentPrice_feature_div">
                <span class="a-price">
                  <span class="a-offscreen">1.142,00 TL</span>
                </span>
                <span>12 taksit</span>
              </div>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.com.tr/dp/B0D6YXHYCX",
    )

    assert result.success is False
    assert result.error == "price_not_found"


def test_amazon_adapter_extracts_total_price_block_when_price_to_pay_markup_varies() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Amazon Product">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="tp_price_block_total_price_ww">
              <span class="a-offscreen">13.702,00 TL</span>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.com.tr/dp/B0D6YXHYCX",
    )

    assert result.success is True
    assert result.amount == "13702.00"
    assert result.currency == "TRY"
    assert result.raw_text == "13.702,00 TL"


def test_extract_price_from_page_returns_stock_only_when_amazon_is_unavailable() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Ninja CREAMi 7-in-1 Dondurma Makinesi">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="availabilityInsideBuyBox_feature_div">
              <div id="availability">
                <span>Su anda mevcut degil.</span>
              </div>
            </div>
            <div class="widgets">
              <span class="a-price"><span class="a-offscreen">9.999,00 TL</span></span>
              <span class="price">11.999,00 TL</span>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.com.tr/Ninja-CREAMi-7-in-1-Dondurma-Makinesi/dp/B0G26N7D9N?th=1",
    )

    assert result.success is True
    assert result.amount is None
    assert result.availability == "out_of_stock"
    assert result.listing_count == 0
    assert result.source is None


def test_extract_price_from_page_returns_stock_only_when_amazon_is_unavailable_in_spanish() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Ninja CREAMi 7-in-1 Dondurma Makinesi">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="availabilityInsideBuyBox_feature_div">
              <div id="availability">
                <span>Actualmente no disponible.</span>
              </div>
            </div>
            <div class="widgets">
              <span class="a-price"><span class="a-offscreen">9.999,00 TL</span></span>
              <span class="price">11.999,00 TL</span>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.es/Ninja-CREAMi-7-in-1-Dondurma-Makinesi/dp/B0G26N7D9N?th=1",
    )

    assert result.success is True
    assert result.amount is None
    assert result.availability == "out_of_stock"
    assert result.listing_count == 0
    assert result.source is None


def test_extract_price_from_page_returns_stock_only_when_amazon_is_unavailable_in_french() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Ninja CREAMi 7-in-1 Dondurma Makinesi">
            <meta property="og:image" content="https://m.media-amazon.com/images/I/example.jpg">
          </head>
          <body>
            <div id="availabilityInsideBuyBox_feature_div">
              <div id="availability">
                <span>Actuellement indisponible.</span>
              </div>
            </div>
            <div class="widgets">
              <span class="a-price"><span class="a-offscreen">9.999,00 TL</span></span>
            </div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(
        page,
        "https://www.amazon.fr/Ninja-CREAMi-7-in-1-Dondurma-Makinesi/dp/B0G26N7D9N?th=1",
    )

    assert result.success is True
    assert result.amount is None
    assert result.availability == "out_of_stock"
    assert result.listing_count == 0
    assert result.source is None


def test_extract_price_from_page_returns_stock_only_for_non_amazon_adapter_when_oos() -> None:
    page = Selector(
        """
        <html>
          <head>
            <meta property="og:title" content="Target Product">
            <meta property="og:image" content="https://target.scene7.com/image/example">
          </head>
          <body>
            <div data-stock-status="sold out">Sold out</div>
            <div data-test='product-price'>$149.99</div>
          </body>
        </html>
        """
    )

    result = extract_price_from_page(page, "https://www.target.com/p/sample-product/-/A-123456")

    assert result.success is True
    assert result.amount is None
    assert result.availability == "out_of_stock"
    assert result.listing_count == 0
    assert result.source is None


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


def test_fetch_price_marks_out_of_stock_metadata_as_success_without_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scraper import service

    page = Selector(
        """
        <html>
          <head>
            <meta property='og:title' content='Sold Out Product'>
            <meta property='og:image' content='https://cdn.example.com/oos.jpg'>
          </head>
          <body>
            <div class='availability'>Currently unavailable</div>
          </body>
        </html>
        """
    )
    monkeypatch.setattr(service, "fetch_page_with_mode", lambda *_args, **_kwargs: page)

    result = fetch_price("https://example.com/products/oos", fetch_mode="http")

    assert result.success is True
    assert result.amount is None
    assert result.availability == "out_of_stock"
    assert missing_required_result_fields(result) == []
    assert is_result_qualified(result) is True


def test_extract_product_metadata_normalizes_common_out_of_stock_phrases() -> None:
    metadata = extract_product_metadata(
        Selector(
            """
            <html>
              <head>
                <meta property='og:title' content='Test Product'>
                <meta property='og:image' content='https://cdn.example.com/test.jpg'>
              </head>
              <body>
                <div class='availability'>Currently unavailable</div>
              </body>
            </html>
            """
        ),
        "https://example.com/products/availability",
    )

    assert metadata["availability"] == "out_of_stock"


def test_extract_product_metadata_normalizes_stokta_yok_phrase_as_out_of_stock() -> None:
    metadata = extract_product_metadata(
        Selector(
            """
            <html>
              <head>
                <meta property='og:title' content='Test Product'>
                <meta property='og:image' content='https://cdn.example.com/test.jpg'>
              </head>
              <body>
                <div class='availability'>Stokta yok</div>
              </body>
            </html>
            """
        ),
        "https://example.com/products/availability",
    )

    assert metadata["availability"] == "out_of_stock"


def test_extract_product_metadata_normalizes_data_stock_status_attribute() -> None:
    metadata = extract_product_metadata(
        Selector(
            """
            <html>
              <head>
                <meta property='og:title' content='Test Product'>
                <meta property='og:image' content='https://cdn.example.com/test.jpg'>
              </head>
              <body>
                <div data-stock-status="sold out">Sold out</div>
              </body>
            </html>
            """
        ),
        "https://example.com/products/availability",
    )

    assert metadata["availability"] == "out_of_stock"


def test_extract_product_metadata_normalizes_spanish_out_of_stock_phrase() -> None:
    metadata = extract_product_metadata(
        Selector(
            """
            <html>
              <head>
                <meta property='og:title' content='Producto de Prueba'>
                <meta property='og:image' content='https://cdn.example.com/test.jpg'>
              </head>
              <body>
                <div class='availability'>Actualmente no disponible</div>
              </body>
            </html>
            """
        ),
        "https://example.com/products/availability",
    )

    assert metadata["availability"] == "out_of_stock"


@pytest.mark.parametrize(
    ("availability_text", "expected"),
    [
        ("Rupture de stock", "out_of_stock"),  # French
        ("Nicht auf Lager", "out_of_stock"),  # German
        ("Esaurito", "out_of_stock"),  # Italian
        ("Esgotado", "out_of_stock"),  # Portuguese
        ("Niet op voorraad", "out_of_stock"),  # Dutch
        ("Brak w magazynie", "out_of_stock"),  # Polish
        ("Нет в наличии", "out_of_stock"),  # Russian
        ("Немає в наявності", "out_of_stock"),  # Ukrainian
        ("缺货", "out_of_stock"),  # Chinese
        ("在庫切れ", "out_of_stock"),  # Japanese
        ("품절", "out_of_stock"),  # Korean
        ("غير متوفر", "out_of_stock"),  # Arabic
        ("במלאי", "in_stock"),  # Hebrew
        ("Auf Lager", "in_stock"),  # German in-stock
        ("En stock", "in_stock"),  # French/Spanish in-stock
        ("预售", "preorder"),  # Chinese preorder
        ("Предзаказ", "preorder"),  # Russian preorder
        ("Discontinuado", "discontinued"),  # Spanish discontinued
        ("Eingestellt", "discontinued"),  # German discontinued
    ],
)
def test_extract_product_metadata_normalizes_multilingual_availability(
    availability_text: str,
    expected: str,
) -> None:
    metadata = extract_product_metadata(
        Selector(
            f"""
            <html>
              <head>
                <meta property='og:title' content='Global Availability Product'>
                <meta property='og:image' content='https://cdn.example.com/test.jpg'>
              </head>
              <body>
                <div class='availability'>{availability_text}</div>
              </body>
            </html>
            """
        ),
        "https://example.com/products/availability",
    )

    assert metadata["availability"] == expected


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
