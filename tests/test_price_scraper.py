from scrapling.parser import Selector

from decimal import Decimal

import pytest

from price_scraper import (
    AkakceAdapter,
    PriceCandidate,
    _make_item_id,
    detect_site_key,
    extract_price_listings_from_selector,
    parse_sites_blob,
    parse_urls_blob,
)


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

    first = _make_item_id("https://example.com/product", candidate, 1)
    second = _make_item_id("https://example.com/product", candidate, 99)

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

    first = _make_item_id("https://example.com/product", first_candidate, 1)
    second = _make_item_id("https://example.com/product", second_candidate, 1)

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

    first = _make_item_id("https://www.akakce.com/example,123.html", first_candidate, 1)
    second = _make_item_id("https://www.akakce.com/example,123.html", second_candidate, 1)

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


def test_parse_sites_blob_invalid_key() -> None:
    with pytest.raises(ValueError):
        parse_sites_blob("akakce,unknownsite")


def test_detect_site_key() -> None:
    assert detect_site_key("https://www.akakce.com/ssd/a") == "akakce"
    assert detect_site_key("https://newegg.com/p/pl?d=4070") == "newegg"
    assert detect_site_key("https://example.com/product") == "generic"
