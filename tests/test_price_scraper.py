from __future__ import annotations

from decimal import Decimal

import pytest
from scrapling.parser import Selector

from app.scraper.adapters import AkakceAdapter
from app.scraper.domain import PriceCandidate
from app.scraper.extractors.generic import extract_price_listings_from_selector
from app.scraper.fetching import auto_followup_modes, detect_auto_escalation_signals
from app.scraper.inputs import parse_fetch_mode, parse_sites_blob, parse_urls_blob
from app.scraper.metadata import extract_product_metadata
from app.scraper.parsing import make_item_id
from app.scraper.registry import ADAPTERS, detect_site_key, list_supported_sites
from app.scraper.service import extract_price_from_page


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
    assert auto_followup_modes(protection_signals) == ["stealth", "dynamic"]
    assert auto_followup_modes(js_signals) == ["dynamic", "stealth"]


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
