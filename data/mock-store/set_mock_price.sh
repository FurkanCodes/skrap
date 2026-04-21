#!/usr/bin/env bash
set -euo pipefail

PRICE="${1:-1299.00}"
ITEM_NAME="${2:-Aurora Smart Desk Lamp}"
SKU="${3:-ASDL-$(date +%s)}"
NOW="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

cat > data/mock-store/product.html <<HTML
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${ITEM_NAME} - Test Store</title>
  <meta property="og:title" content="${ITEM_NAME}" />
  <meta property="og:image" content="https://picsum.photos/seed/${SKU}/600/600" />
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "${ITEM_NAME}",
    "sku": "${SKU}",
    "image": "https://picsum.photos/seed/${SKU}/600/600",
    "offers": {
      "@type": "Offer",
      "priceCurrency": "TRY",
      "price": "${PRICE}",
      "url": "http://127.0.0.1:8099/product.html"
    }
  }
  </script>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; }
    .card { border: 1px solid #ddd; border-radius: 14px; padding: 20px; }
    .price { font-size: 38px; font-weight: 800; margin: 12px 0; }
    .meta { color: #555; font-size: 14px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>${ITEM_NAME}</h1>
    <div class="meta">SKU: ${SKU}</div>
    <div class="price">₺${PRICE}</div>
    <div class="meta">Last updated: ${NOW}</div>
  </div>
</body>
</html>
HTML

echo "Wrote data/mock-store/product.html with price=${PRICE} item=${ITEM_NAME} sku=${SKU}"
