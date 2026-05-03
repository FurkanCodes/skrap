from __future__ import annotations

from html import escape

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.scraper.parsing import parse_decimal

router = APIRouter(tags=["debug"])

_VALID_STOCK_STATES = {"in_stock", "out_of_stock", "preorder", "discontinued"}


def _availability_label(stock: str) -> str:
    if stock == "out_of_stock":
        return "Currently unavailable"
    if stock == "preorder":
        return "Available for pre-order"
    if stock == "discontinued":
        return "Discontinued"
    return "In Stock"


def _schema_availability(stock: str) -> str:
    if stock == "out_of_stock":
        return "https://schema.org/OutOfStock"
    if stock == "preorder":
        return "https://schema.org/PreOrder"
    if stock == "discontinued":
        return "https://schema.org/Discontinued"
    return "https://schema.org/InStock"


def _resolved_stock(stock: str) -> str:
    normalized = stock.strip().lower()
    if normalized in _VALID_STOCK_STATES:
        return normalized
    return "in_stock"


def _resolved_price(price: str) -> str:
    parsed = parse_decimal(price.strip())
    if parsed is None:
        return "9999.00"
    return format(parsed, "f")


@router.get("/debug/test-product", response_class=HTMLResponse, include_in_schema=False)
def get_debug_test_product(
    request: Request,
    price: str = Query(default="9999.00"),
    stock: str = Query(default="in_stock"),
    currency: str = Query(default="TRY", min_length=3, max_length=6),
    name: str = Query(default="Skrap Test Product"),
    sku: str = Query(default="SKRAP-TEST-001"),
    include_noise: bool = Query(default=True),
) -> HTMLResponse:
    resolved_price = _resolved_price(price)
    resolved_stock = _resolved_stock(stock)
    availability_label = _availability_label(resolved_stock)
    schema_availability = _schema_availability(resolved_stock)
    canonical_url = str(request.url.replace(query=""))
    escaped_name = escape(name.strip() or "Skrap Test Product")
    escaped_sku = escape(sku.strip() or "SKRAP-TEST-001")
    escaped_currency = escape((currency.strip() or "TRY").upper())
    image_url = "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=1200&q=80"

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_name}</title>
  <link rel="canonical" href="{canonical_url}">
  <meta property="og:title" content="{escaped_name}">
  <meta property="og:image" content="{image_url}">
  <meta property="og:url" content="{canonical_url}">
  <meta itemprop="name" content="{escaped_name}">
  <meta itemprop="sku" content="{escaped_sku}">
  <meta itemprop="priceCurrency" content="{escaped_currency}">
  <meta itemprop="price" content="{resolved_price}">
  <meta property="product:price:currency" content="{escaped_currency}">
  <meta property="product:price:amount" content="{resolved_price}">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif;
      margin: 0;
      background: #f7f7f8;
      color: #101114;
    }}
    main {{
      max-width: 920px;
      margin: 24px auto;
      padding: 0 16px 32px;
      display: grid;
      gap: 16px;
    }}
    section {{
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 16px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 24px;
      line-height: 1.2;
    }}
    .row {{
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .price {{
      font-size: 32px;
      font-weight: 700;
    }}
    .availability {{
      font-size: 16px;
      font-weight: 600;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }}
    label {{
      display: grid;
      gap: 6px;
      font-size: 12px;
      color: #374151;
    }}
    input, select {{
      height: 36px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      padding: 0 10px;
      font-size: 14px;
    }}
    button {{
      height: 36px;
      border: 1px solid #111827;
      border-radius: 6px;
      background: #111827;
      color: #fff;
      padding: 0 12px;
      font-size: 14px;
      cursor: pointer;
    }}
    .noise {{
      color: #6b7280;
      font-size: 14px;
    }}
  </style>
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "{escaped_name}",
    "sku": "{escaped_sku}",
    "image": "{image_url}",
    "offers": {{
      "@type": "Offer",
      "priceCurrency": "{escaped_currency}",
      "price": "{resolved_price}",
      "availability": "{schema_availability}",
      "url": "{canonical_url}"
    }}
  }}
  </script>
</head>
<body>
  <main>
    <section>
      <h1 id="productTitle">{escaped_name}</h1>
      <div class="row">
        <img src="{image_url}" alt="{escaped_name}" width="180" height="180">
        <div>
          <div id="corePrice_feature_div">
            <span class="a-price">
              <span class="a-offscreen">{resolved_price} {escaped_currency}</span>
            </span>
          </div>
          <div class="price" itemprop="price">{resolved_price} {escaped_currency}</div>
          <div id="availability" class="availability">{availability_label}</div>
          <link itemprop="availability" href="{schema_availability}">
          <div>SKU: {escaped_sku}</div>
        </div>
      </div>
    </section>

    <section>
      <form method="get" action="/debug/test-product">
        <div class="grid">
          <label>
            Product Name
            <input name="name" value="{escape(name)}">
          </label>
          <label>
            SKU
            <input name="sku" value="{escape(sku)}">
          </label>
          <label>
            Price
            <input name="price" value="{escape(price)}">
          </label>
          <label>
            Currency
            <input name="currency" value="{escape(currency)}">
          </label>
          <label>
            Stock
            <select name="stock">
              <option value="in_stock"{" selected" if resolved_stock == "in_stock" else ""}>in_stock</option>
              <option value="out_of_stock"{" selected" if resolved_stock == "out_of_stock" else ""}>out_of_stock</option>
              <option value="preorder"{" selected" if resolved_stock == "preorder" else ""}>preorder</option>
              <option value="discontinued"{" selected" if resolved_stock == "discontinued" else ""}>discontinued</option>
            </select>
          </label>
          <label>
            Noise Prices
            <select name="include_noise">
              <option value="true"{" selected" if include_noise else ""}>true</option>
              <option value="false"{" selected" if not include_noise else ""}>false</option>
            </select>
          </label>
        </div>
        <div style="margin-top: 12px;">
          <button type="submit">Apply</button>
        </div>
      </form>
    </section>
"""
    if include_noise:
        html += """
    <section>
      <div class="noise">Intentional noise block: unrelated price nodes for robustness testing.</div>
      <ul>
        <li><span class="a-price"><span class="a-offscreen">11999.00 TRY</span></span></li>
        <li><span class="a-price"><span class="a-offscreen">14999.00 TRY</span></span></li>
        <li><span class="price">7699.00 TRY</span></li>
      </ul>
    </section>
"""
    html += """
  </main>
</body>
</html>
"""
    return HTMLResponse(content=html)
