# Product Price Scraper (Scrapling)

This project scrapes product prices from pasted product URLs.
It returns all detected listings per URL (single-offer stores or multi-offer comparison pages).
Each product response includes a stable `product_id`, and each listing includes a stable `item_id` for alarms/watchlists.

## 1) CLI usage

Use the project virtualenv Python:

```bash
./.venv/bin/python price_scraper.py --json https://example.com/product-1
```

List supported adapters:

```bash
./.venv/bin/python price_scraper.py --list-sites
```

Filter scraping to selected adapters:

```bash
./.venv/bin/python price_scraper.py --sites akakce,newegg --json https://www.akakce.com/...
```

Or paste many links via stdin:

```bash
cat <<'EOF' | ./.venv/bin/python price_scraper.py
https://example.com/product-1
https://example.com/product-2
EOF
```

## 2) REST API (FastAPI)

Install dependencies:

```bash
./.venv/bin/pip install -r requirements.txt
```

Run the API server:

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open API docs:

- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>

Main endpoints:

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
    "timeout_seconds": 30
  }'
```

## 3) Paste-links web UI

Run a local server:

```bash
./.venv/bin/python paste_links_server.py --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>, paste links (one per line), and submit.
You can optionally set a site filter in the `sites` input (example: `akakce,newegg`).

## Notes

- Adapter flow: domain -> site adapter (`akakce` / `newegg`) -> optional generic fallback.
- Generic extraction order: JSON-LD listings -> DOM listings -> `find/find_all` fallback -> meta tags -> page-text fallback.
- Some websites hide prices behind JavaScript/anti-bot checks. In those cases, dynamic fetchers may be needed.

## 4) Add a new site adapter

1. In `price_scraper.py`, create a new adapter class inheriting `SiteAdapter`.
2. Register the key in `SUPPORTED_SITES`, map domains in `SITE_DOMAIN_SUFFIXES`, and add the adapter instance to `ADAPTERS`.
3. Keep the adapter returning a `list[PriceCandidate]` so IDs/listings logic stays shared.
4. Add tests in `tests/test_price_scraper.py` for parser behavior and ID stability.
