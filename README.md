# Product Price Scraper API (Scrapling)

This project provides a REST API that scrapes product prices from URLs.
Each scrape response returns:

- a stable `product_id` per input URL
- one or more price listings with stable `item_id` values

## Run the API

Install dependencies:

```bash
./.venv/bin/pip install -r requirements.txt
```

Start the server:

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Docs:

- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>

## Endpoints

- `GET /health`
- `GET /api/v1/sites`
- `POST /api/v1/scrapes`

Example scrape request:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/scrapes \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://example.com/product-1", "https://example.com/product-2"],
    "sites": ["akakce", "newegg"],
    "timeout_seconds": 30,
    "fetch_mode": "auto"
  }'
```

## Scraper Architecture

The scraper internals are under `app/scraper`:

- `domain.py`: result/value models (`PriceCandidate`, `PriceListing`, `PriceResult`)
- `parsing.py`: currency, decimal, text parsing, URL normalization, ID generation
- `fetching.py`: fetch-mode routing and auto-escalation signal detection
- `extractors/generic.py`: generic extraction pipeline and fallback order
- `adapters.py`: site-specific adapters (`generic`, `akakce`, `newegg`)
- `registry.py`: supported sites, domain suffix routing, adapter registry
- `service.py`: orchestrates `fetch_price` / `scrape_prices`

## Notes

- Adapter flow: domain -> site adapter (`akakce` / `newegg`) -> optional generic fallback.
- Auto fetch flow: HTTP `Fetcher` first, then browser fetchers (`DynamicFetcher`/`StealthyFetcher`) when JS/protection signals are detected.
- Generic extraction order: JSON-LD listings -> DOM listings -> `find/find_all` fallback -> meta tags -> page-text fallback.
