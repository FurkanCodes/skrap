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
# Put TRACKING_DATABASE_URL in .env (recommended) or export it in shell.
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Docs:

- Swagger UI: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>

## Endpoints

- `GET /health`
- `GET /api/v1/sites`
- `POST /api/v1/scrapes`
- `POST /api/v1/tracked-items`
- `GET /api/v1/tracked-items`
- `GET /api/v1/tracked-items/{item_id}`
- `PATCH /api/v1/tracked-items/{item_id}`
- `DELETE /api/v1/tracked-items/{item_id}`

Tracked-item bootstrap supports optional idempotent retries via request header:

- `Idempotency-Key: <client-generated-unique-key>`

## Periodic Scheduler (Phase 1)

Run periodic tracking checks in a separate process:

```bash
# .env is auto-loaded by app config.
./.venv/bin/python -m app.jobs.scheduler
```

Run notification delivery worker in another process:

```bash
./.venv/bin/python -m app.jobs.notifier
```

Useful scheduler env vars:

- `SCHEDULER_POLL_SECONDS` (default: `10`)
- `SCHEDULER_CLAIM_BATCH_SIZE` (default: `250`)
- `SCHEDULER_MAX_INFLIGHT` (default: `64`)
- `SCHEDULER_LEASE_SECONDS` (default: `180`)
- `SCHEDULER_JITTER_PERCENT` (default: `10`)
- `SCHEDULER_ALERT_COOLDOWN_MINUTES` (default: `60`, set `0` to disable cooldown)
- `SCHEDULER_LEASE_OWNER` (optional explicit worker id)

Notifier env vars:

- `NOTIFIER_POLL_SECONDS` (default: `5`)
- `NOTIFIER_CLAIM_BATCH_SIZE` (default: `100`)
- `NOTIFIER_LEASE_SECONDS` (default: `120`)
- `NOTIFIER_MAX_ATTEMPTS` (default: `5`)
- `NOTIFIER_BASE_BACKOFF_SECONDS` (default: `30`)
- `NOTIFIER_BACKOFF_MAX_SECONDS` (default: `3600`)
- `NOTIFIER_RETRY_JITTER_PERCENT` (default: `10`)
- `NOTIFIER_LEASE_OWNER` (optional explicit worker id)

Database/pool env vars:

- `TRACKING_DATABASE_URL` (preferred, Supabase Postgres pooler URL)
- `SUPABASE_DB_URL` / `DATABASE_URL` (fallback aliases)
- `TRACKING_DB_POOL_MIN_SIZE` (default: `1`)
- `TRACKING_DB_POOL_MAX_SIZE` (default: `10`)

## SQLite -> Supabase Migration

If you already have local SQLite tracking data, migrate it once:

```bash
export TRACKING_DATABASE_URL="postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require"
./.venv/bin/python -m app.jobs.migrate_sqlite_to_postgres \
  --sqlite-path ./data/tracking.sqlite3 \
  --truncate
```

Notes:

- `--database-url` can be passed explicitly to override env var resolution.
- With `--truncate`, the script verifies source/target row counts and fails if they differ.
- Use `--skip-verify` only when you intentionally allow non-equal counts.
- Migration now includes tracking + alerting tables:
  - `tracked_items`, `price_snapshots`, `tracking_check_runs`
  - `tracking_bootstrap_idempotency`, `alert_events`, `notification_events`
- Full runbook (backup, rollback, validation):
  - [/Users/furkan/skrap/docs/postgres-migration-runbook.md](/Users/furkan/skrap/docs/postgres-migration-runbook.md)

Scheduler logs include structured cycle + rolling-window metrics:

- cycle counts (`claimed`, `processed`, `skipped`, `success`, `failure`)
- due-lag percentiles (`cycle_due_lag_p50/p95/p99`)
- one-hour rolling due-lag p95 (`window_due_lag_p95`)
- per-site success/failure counters
- failure class counters (`price_not_found`, `timeout`, `blocked`, `duplicate_job`, etc.)

Scheduler dedupe semantics:

- job execution idempotency key = `item_id + scheduled_for`
- duplicate jobs for the same key are skipped
- snapshot writes are deduped per `(item_id, scheduled_for)`

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

`/api/v1/scrapes` now returns:

- `scrape_ref` at batch level
- `result_ref` for each result row

You can create tracked items without a second scrape by posting:

- `POST /api/v1/tracked-items` with `scrape_result_ref` (no `url` required)

Cache tuning env vars:

- `SCRAPE_RESULT_CACHE_TTL_SECONDS` (default: `900`)
- `SCRAPE_RESULT_CACHE_MAX_BATCHES` (default: `500`)

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

## Backend Design Doc

- Delivery-framework architecture and roadmap:
  - `/Users/furkan/skrap/docs/backend-architecture-delivery.md`
