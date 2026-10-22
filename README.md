# Skrap Backend API

Backend service for product price extraction, URL discovery, item tracking, and periodic re-checking.

This README is written as a UI-facing API contract guide.
It documents every public endpoint, payload shape, validation rule, and the expected frontend usage flows.

Production deployment runbook: [Dealvinci Coolify Production Setup](docs/deploy-coolify-dealvinci.md).

## Live demo

Try it at **https://skrap-ui.vercel.app** with `demo@tilvg.app` / `TilvgDemo-2026!` (shared account, please don't change the password).
The demo runs on free tiers ([setup](docs/deploy-free-demo.md)): payments, automatic re-checks/alerts, and social sign-in are disabled,
and the API sleeps when idle, so the first request can take about a minute.

## Table of Contents
1. [What This API Does](#what-this-api-does)
2. [Architecture at a Glance](#architecture-at-a-glance)
3. [Quick Start](#quick-start)
4. [API Conventions](#api-conventions)
5. [Endpoint Reference](#endpoint-reference)
6. [UI Integration Flows](#ui-integration-flows)
7. [Background Workers (Scheduler + Notifier)](#background-workers-scheduler--notifier)
8. [Data Model (Tracking Domain)](#data-model-tracking-domain)
9. [Configuration](#configuration)
10. [Operational Notes and Edge Cases](#operational-notes-and-edge-cases)
11. [Current Limitations](#current-limitations)

## What This API Does

Core capabilities:
- Scrape one or more product URLs and return normalized price data.
- Discover related product/pagination links from seed URLs, then scrape discovered URLs.
- Create tracked items from either:
  - a direct URL, or
  - a previous scrape result reference (`scrape_result_ref`) without re-scraping.
- Persist historical snapshots for tracked items.
- Periodically re-check active tracked items in a separate scheduler process.
- Emit alert events when price crosses below target and enqueue notification events.
- Deliver notifications through a notifier worker (MVP sender logs payloads).

Out of scope for now:
- Authentication/authorization.
- User management.
- Real provider integrations (email/SMS/push) beyond logging sender.

## Architecture at a Glance

```mermaid
flowchart LR
    UI["UI / Client"] --> API["FastAPI"]

    API --> SCRAPES["/scrapes"]
    API --> DISCOVER["/discover"]
    API --> TRACK["/tracked-items"]

    SCRAPES --> SPIDER1["PriceScrapeSpider\n(seed URLs only)"]
    DISCOVER --> SPIDER2["PriceDiscoverySpider\n(same-domain crawl)"]

    SPIDER1 --> EXTRACT["Adapter + Generic extraction pipeline"]
    SPIDER2 --> EXTRACT

    TRACK --> DB[(PostgreSQL)]
    EXTRACT --> DB

    SCHED["Scheduler process"] --> DB
    NOTIF["Notifier process"] --> DB
```

Key implementation detail:
- `/api/v1/scrapes` already uses Scrapling Spider runtime (`PriceScrapeSpider`) but **does not follow links**.
- `/api/v1/discover` uses a separate discovery spider (`PriceDiscoverySpider`) that can follow same-domain pagination/product-like links.

## Quick Start

### 1. Install dependencies

```bash
./.venv/bin/pip install -r requirements.txt
```

### 2. Configure environment

Use explicit env profiles instead of a mixed `.env`:

```bash
cp .env.local.example .env.local
cp .env.production.example .env.production
```

Notes:
- Local runs load `.env.local` first, then fall back to `.env`.
- Production runs should provide process env or use `.env.production`.
- Docker compose uses `.env.production` by default. Override with `ENV_FILE=.env.local`.
- Fallback env resolution order for DB URL:
  1. `TRACKING_DATABASE_URL`
  2. `SUPABASE_DB_URL`
  3. `DATABASE_URL`
  4. default local postgres URL

### 3. Start API

Start the full local backend stack in one terminal:

```bash
./.venv/bin/python scripts/dev_backend.py
```

This starts the API, scheduler dispatcher, scheduler worker, and notifier, and stops all of them when you press `Ctrl+C`.

Or run each process manually:

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 4. Start background workers in separate terminals

```bash
./.venv/bin/python -m app.jobs.scheduler
```

```bash
./.venv/bin/python -m app.jobs.notifier
```

Queue split mode (recommended for scale):

```bash
./.venv/bin/python -m app.jobs.scheduler_dispatcher
```

```bash
./.venv/bin/python -m app.jobs.scheduler_worker
```

### 5. Open docs

- Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- ReDoc: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

## API Conventions

### Base path
- Health endpoint: `/health`
- Versioned API prefix: `/api/v1` (configurable via `API_PREFIX`)

### Content type
- Request: `application/json`
- Response: `application/json` (except `DELETE /tracked-items/{item_id}` returns `204 No Content`)

### Request ID
- Middleware sets `X-Request-ID` response header for every request.
- Client may send `X-Request-ID`; if missing, server generates one.
- Most write responses include `request_id` in body.

### Time format
- ISO-8601 UTC (`...Z`) in payloads, e.g. `2026-04-17T16:26:31.058488Z`.

### Money format
- `amount` and `target_amount` are strings.
- Current implementation expects decimal-like strings (e.g. `"47999.00"`).
- UI should treat them as decimal text, not float.

### Tracking modes
Derived from `target_amount`:
- `target_amount == null` (or blank) => `tracking_mode: "monitor"`, `alerts_enabled: false`
- `target_amount != null` and non-empty => `tracking_mode: "alert"`, `alerts_enabled: true`

### Error model
- Validation/semantic errors: `422` with `{"detail": ...}`
- Missing resource: `404`
- Idempotency conflict: `409`
- Discovery-phase operational failure: `502`

## Endpoint Reference

## GET /health

Lightweight liveness check.

### Response

```json
{
  "status": "ok"
}
```

## GET /api/v1/me

Returns current user profile, active entitlement limits, and current monthly credit usage.

Auth:
- Requires valid bearer token when `AUTH_REQUIRED=true`.
- In local/dev mode (`AUTH_REQUIRED=false`), returns deterministic `free` defaults.

### Response

```json
{
  "user": {
    "user_id": "usr_...",
    "auth_provider": "better_auth",
    "auth_subject": "auth_sub_...",
    "email": "user@example.com",
    "display_name": "User Name",
    "created_at": "...Z",
    "updated_at": "...Z"
  },
  "entitlement": {
    "user_id": "usr_...",
    "plan_key": "free",
    "max_active_trackers": 5,
    "min_check_interval_minutes": 720,
    "monthly_check_units": 100,
    "allowed_channels": "telegram",
    "overage_policy": "block",
    "created_at": "...Z",
    "updated_at": "...Z"
  },
  "usage": {
    "user_id": "usr_...",
    "period_start": "2026-04-01T00:00:00Z",
    "period_end": "2026-05-01T00:00:00Z",
    "monthly_check_units": 100,
    "consumed_check_units": 240,
    "remaining_check_units": 260
  }
}
```

UI usage:
- Use `entitlement.max_active_trackers` to cap create-track CTA.
- Use `entitlement.min_check_interval_minutes` as minimum value in interval selector.
- Show credit meter with `usage.consumed_check_units` and `usage.remaining_check_units`.
- Use `usage.period_end` for "resets on" messaging.

## Locked Plan Limits

Current launch limits:

| Plan | `plan_key` | Max active trackers | Fastest interval | Monthly credits (`monthly_check_units`) | Channels | Overage |
|---|---|---:|---:|---:|---|---|
| Free | `free` | 5 | 720 min | 100 | telegram | block |
| Plus | `plus` | 25 | 120 min | 10000 | telegram | block |
| Pro | `pro` | 100 | 30 min | 50000 | telegram | block |

Credit rule:
- 1 credit = 1 scheduled product re-check.
- Credits are consumed by scheduler checks, not by browsing UI.

## GET /api/v1/sites

Returns supported site adapter keys.

### Response

```json
{
  "sites": [
    {"key": "akakce", "description": "Akakce adapter"},
    {"key": "generic", "description": "Generic extractor (fallback for any site)"},
    {"key": "newegg", "description": "Newegg adapter"}
  ]
}
```

UI usage:
- Populate site-filter chips/dropdown.
- Use `generic` as default safe option.

## POST /api/v1/scrapes

Batch scrape endpoint for direct URL list.

### Request body

| Field | Type | Required | Notes |
|---|---|---:|---|
| `urls` | `list[AnyHttpUrl]` | yes | min 1, max controlled by `SCRAPE_MAX_URLS` |
| `sites` | `list[str] \| null` | no | site filter; accepts `all` or `*` wildcard |
| `timeout_seconds` | `int \| null` | no | defaults to server default; must be within min/max bounds |
| `fetch_mode` | `"auto" \| "http" \| "dynamic" \| "stealth"` | no | default `auto` |

### Important behaviors
- URLs are de-duplicated preserving input order.
- `/scrapes` uses `PriceScrapeSpider` with seed URLs only (no link traversal).
- In `auto` mode, scraper starts with HTTP and can escalate to dynamic/stealth when signals indicate JS/protection.

### Example request

```json
{
  "urls": [
    "https://www.epey.com/akilli-telefonlar/huawei-pura-70.html"
  ],
  "sites": ["generic"],
  "timeout_seconds": 20,
  "fetch_mode": "auto"
}
```

### Response body

| Field | Type | Description |
|---|---|---|
| `request_id` | `str` | request correlation id |
| `scrape_ref` | `str \| null` | in-memory cache ref for this batch |
| `total_urls` | `int` | de-duplicated URL count |
| `success_count` | `int` | successful extraction count |
| `failure_count` | `int` | failed extraction count |
| `results` | `PriceResultResponse[]` | one result per de-duplicated input URL |

### `PriceResultResponse` fields

| Field | Type | Description |
|---|---|---|
| `result_ref` | `str \| null` | reference for track bootstrap without re-scrape |
| `url` | `str` | final URL used for this result |
| `success` | `bool` | extraction success flag |
| `product_id` | `str \| null` | stable product hash id for URL |
| `site_key` | `str \| null` | detected site key |
| `adapter_key` | `str \| null` | adapter that produced result (`generic`, `akakce`, etc.) |
| `amount` | `str \| null` | top listing amount |
| `currency` | `str \| null` | detected currency |
| `raw_text` | `str \| null` | raw extracted price text |
| `source` | `str \| null` | extraction source identifier |
| `product_name` | `str \| null` | extracted metadata |
| `brand` | `str \| null` | extracted metadata |
| `model` | `str \| null` | extracted metadata |
| `sku` | `str \| null` | extracted metadata |
| `gtin` | `str \| null` | extracted metadata |
| `image_url` | `str \| null` | extracted metadata |
| `canonical_url` | `str \| null` | canonicalized product URL when available |
| `availability` | `str \| null` | extracted stock/availability signal |
| `extracted_at` | `str \| null` | extraction timestamp |
| `listing_count` | `int` | number of normalized listings |
| `listings` | `PriceListingResponse[]` | full listing set |
| `error` | `str \| null` | failure reason when `success=false` |

### `PriceListingResponse` fields

| Field | Type | Description |
|---|---|---|
| `item_id` | `str` | stable item id derived from URL + candidate |
| `amount` | `str` | listing amount |
| `currency` | `str \| null` | listing currency |
| `raw_text` | `str` | raw listing text |
| `source` | `str` | extraction source |
| `seller` | `str \| null` | seller/merchant name |
| `listing_url` | `str \| null` | listing URL |

### UI recommendation
- Use `results[].result_ref` for immediate tracking CTA.
- Do not force user to re-enter URL after scraping.

## POST /api/v1/discover

Two-phase endpoint:
1. Discover URLs with spider crawl.
2. Scrape discovered URLs using same normalization pipeline as `/scrapes`.

### Request body

| Field | Type | Required | Notes |
|---|---|---:|---|
| `start_urls` | `list[AnyHttpUrl]` | yes | min 1, max same cap as `/scrapes` |
| `sites` | `list[str] \| null` | no | same site normalization semantics |
| `timeout_seconds` | `int \| null` | no | same timeout bound checks |
| `fetch_mode` | `"auto" \| "http" \| "dynamic" \| "stealth"` | no | applies in phase-2 scrape |
| `max_pages` | `int` | no | default 50, allowed 1..300 |
| `max_depth` | `int` | no | default 2, allowed 0..5 |

### Discovery behaviors
- Same-domain traversal only (derived from `start_urls`).
- Link heuristics follow:
  - pagination-like links,
  - product-like links.
- Denylist paths are skipped (cart, checkout, login, account, etc.).
- Blocked retries switch sid from `http` to lazy `stealth` session.

### Response body

| Field | Type | Description |
|---|---|---|
| `request_id` | `str` | request correlation id |
| `seed_count` | `int` | number of input seeds |
| `discovered_url_count` | `int` | discovered URL count before scrape dedupe |
| `discovered_urls` | `list[str]` | discovered canonical URLs (order preserved) |
| `discovery_stats` | object | crawl stats subset |
| `total_urls` | `int` | URLs sent to phase-2 scrape after de-duplication |
| `success_count` | `int` | phase-2 success count |
| `failure_count` | `int` | phase-2 failure count |
| `results` | `PriceResultResponse[]` | same model as `/scrapes` |

`discovery_stats` fields:
- `requests_count`
- `failed_requests_count`
- `blocked_requests_count`
- `offsite_requests_count`
- `elapsed_seconds`
- `sessions_requests_count`

### Example request

```json
{
  "start_urls": [
    "https://www.epey.com/akilli-telefonlar/huawei-pura-70.html"
  ],
  "sites": ["generic"],
  "timeout_seconds": 5,
  "fetch_mode": "auto",
  "max_pages": 50,
  "max_depth": 0
}
```

### UI guidance for product-tracking scenario
- If user wants "just this URL" behavior with spider runtime guarantees, use:
  - `max_depth: 0`
  - typically `max_pages: 1..5`
- If user wants exploration of category/listing pages, increase depth/pages.

### Failure mapping
- If discovery phase crashes: endpoint returns `502` with `discovery_failed: ...` detail.
- If phase-2 scrape crashes internally, service still returns partial failure `results` (status `200`).

## POST /api/v1/tracked-items

Create or refresh a tracked item.

Supports two bootstrap sources:
1. `url` (direct scrape now), or
2. `scrape_result_ref` (reuse recent `/scrapes` result from in-memory cache).

### Headers
- Optional: `Idempotency-Key: <string>`

### Request body

| Field | Type | Required | Notes |
|---|---|---:|---|
| `url` | `AnyHttpUrl \| null` | conditional | required when `scrape_result_ref` absent |
| `scrape_result_ref` | `str \| null` | conditional | required when `url` absent |
| `sites` | `list[str] \| null` | no | same site filter semantics |
| `timeout_seconds` | `int \| null` | no | same timeout bounds |
| `fetch_mode` | `"auto" \| "http" \| "dynamic" \| "stealth"` | no | default `auto` |
| `check_interval_minutes` | `int` | no | default 60, allowed `max(5, user_plan_min)` .. 10080 |
| `target_amount` | `str \| null` | no | null/blank => monitor mode |
| `notify_on_price_increase` | `bool \| null` | no | alert-mode opt-in for extra increase notifications; monitor mode notifies on all price changes by default |
| `notify_on_stock_change` | `bool \| null` | no | opt-in stock out/restock notifications |
| `duplicate_policy` | `"upsert" \| "reject"` | no | default `upsert`; use `reject` to return `409` instead of refreshing an existing URL |

### Important behaviors
- If `scrape_result_ref` is provided, server resolves it from short-lived cache.
  - invalid/expired => `422 Invalid or expired scrape_result_ref`
- Upsert key is `source_url`.
  - existing row for URL is refreshed, not duplicated.
- With `duplicate_policy: "reject"`, an existing row for URL returns `409` with the existing item and does not refresh the row or append a snapshot.
- On successful bootstrap, one snapshot is appended.
- Interval validation is plan-aware:
  - global minimum is 5 minutes
  - effective minimum is `max(global minimum, entitlement.min_check_interval_minutes)`
- With `Idempotency-Key`, same payload can be safely retried.
  - same key + different payload => `409`.

### Example (recommended after `/scrapes`)

```json
{
  "scrape_result_ref": "srf_scf_abc123_0",
  "check_interval_minutes": 60,
  "target_amount": "45000.00"
}
```

### Example (monitor-only item)

```json
{
  "url": "https://www.epey.com/akilli-telefonlar/huawei-pura-70.html",
  "check_interval_minutes": 60,
  "target_amount": null
}
```

### Response

```json
{
  "request_id": "...",
  "item": {
    "item_id": "trk_...",
    "source_url": "https://...",
    "canonical_url": "https://...",
    "product_id": "prd_...",
    "site_key": "generic",
    "adapter_key": "generic",
    "product_name": "...",
    "brand": "...",
    "model": null,
    "sku": null,
    "gtin": null,
    "image_url": "https://...",
    "availability": null,
    "currency": "TRY",
    "last_amount": "47999.00",
    "last_raw_text": "47999.00",
    "last_source": "json-ld[0]",
    "status": "active",
    "tracking_mode": "alert",
    "alerts_enabled": true,
    "check_interval_minutes": 60,
    "target_amount": "45000.00",
    "created_at": "...Z",
    "updated_at": "...Z",
    "last_checked_at": "...Z",
    "next_check_at": "...Z",
    "lease_until": null,
    "last_check_started_at": null,
    "last_check_finished_at": "...Z"
  }
}
```

## GET /api/v1/tracked-items

Returns tracked item list ordered by `updated_at DESC`.

### Response

```json
{
  "items": [
    {
      "item_id": "trk_...",
      "source_url": "https://...",
      "status": "active",
      "tracking_mode": "monitor",
      "alerts_enabled": false,
      "check_interval_minutes": 60,
      "target_amount": null,
      "next_check_at": "...Z",
      "last_checked_at": "...Z"
    }
  ]
}
```

UI usage:
- Main dashboard list.
- Derive badges from `status`, `tracking_mode`, `alerts_enabled`.

## GET /api/v1/tracked-items/lookup

Exact URL lookup for duplicate checks.

### Query params
- `url` (required HTTP(S) URL)

### Response

```json
{
  "item": null
}
```

When the user already tracks the exact `source_url`, `item` is the same tracked item shape returned by list/detail endpoints.

## GET /api/v1/tracked-items/{item_id}

Returns one tracked item + recent snapshots.

### Query params
- `snapshot_limit` (optional int)
  - if omitted: server default (`TRACKING_SNAPSHOT_DEFAULT_LIMIT`)
  - values `<1` are normalized to 1 internally

### Response fields
- `item`: same as list item shape.
- `recent_snapshots`: newest-first snapshot list.

`PriceSnapshotResponse` fields:
- `snapshot_id`
- `item_id`
- `checked_at`
- `success`
- `amount`
- `currency`
- `raw_text`
- `source`
- `error`

## PATCH /api/v1/tracked-items/{item_id}

Partial update of tracking configuration.
No scrape is triggered.

### Request body

| Field | Type | Notes |
|---|---|---|
| `status` | `"active" \| "paused" \| null` | pause/resume tracking |
| `check_interval_minutes` | `int \| null` | must be `max(5, user_plan_min)` .. 10080 when provided |
| `target_amount` | `str \| null` | provide `null` to clear target and switch to monitor mode |

### Behavior notes
- Changing `target_amount` to `null` disables alert mode.
- Monitor mode notifies on confirmed price drops and increases.
- Alert mode notifies when the target is reached; `notify_on_price_increase` can add increase notifications.
- If interval changes, `next_check_at` is recalculated from now.

### Example request

```json
{
  "status": "paused",
  "target_amount": null
}
```

## DELETE /api/v1/tracked-items/{item_id}

Deletes tracked item and related snapshots/check data via FK cascade.

### Response
- `204 No Content`

UI recommendation:
- Offer "Pause" and "Delete" separately.
- Delete is destructive history removal.

## UI Integration Flows

## Flow A: Simple one-off scrape (no tracking)
1. User enters one or many URLs.
2. UI calls `POST /api/v1/scrapes`.
3. Show `results[]` with price + metadata.

## Flow B: Scrape then track without re-scrape (recommended)
1. UI calls `POST /api/v1/scrapes`.
2. User chooses one result row.
3. UI calls `POST /api/v1/tracked-items` with `scrape_result_ref`.
4. Backend stores tracked item directly from cached result.

Why this is preferred:
- avoids duplicated network call,
- lower latency,
- consistent source payload from selected scrape row.

## Flow C: Direct track from URL
1. UI skips `/scrapes`.
2. UI calls `POST /api/v1/tracked-items` with `url`.
3. Backend performs immediate scrape and stores initial snapshot.

Use this when UI is in "quick track" mode and does not need preview list.

## Flow D: Discovery-driven exploration
1. UI calls `POST /api/v1/discover` with seed URL(s).
2. Backend discovers same-domain product/pagination links and scrapes them.
3. UI shows `discovered_urls` and `results`.
4. User picks one result and tracks it (via URL now; optionally via scrape flow/ref strategy).

For strict single-URL scenarios:
- use `max_depth=0` to avoid broader crawling.

## Flow E: Monitor mode vs alert mode

Monitor mode:
- `target_amount = null`
- scheduler still checks periodically,
- snapshots continue,
- no alert event generation.

Alert mode:
- `target_amount = "<decimal>"`
- scheduler emits alert when price crosses from `previous > target` to `current <= target`.

## Flow F: Pause / resume / stop tracking

Pause:
- `PATCH /tracked-items/{item_id}` with `{"status":"paused"}`
- paused items are not claimed by scheduler.

Resume:
- `PATCH /tracked-items/{item_id}` with `{"status":"active"}`

Stop/delete:
- `DELETE /tracked-items/{item_id}`

## Background Workers (Scheduler + Notifier)

These are not HTTP endpoints; they are internal processes.

## Scheduler (`python -m app.jobs.scheduler`)

Responsibilities:
- claim due active items with lease (`claim_due_items`),
- run scrape checks in bounded batches,
- write `tracking_check_runs`,
- update tracked item latest fields,
- append/update snapshots,
- compute `next_check_at` with jitter,
- emit `alert_events` and enqueue `notification_events`.

Execution semantics:
- at-least-once,
- lease-based reclaim on worker crash,
- dedupe key: `item_id + scheduled_for`.

## Scheduler Dispatcher (`python -m app.jobs.scheduler_dispatcher`)

Responsibilities:
- wake on Redis ZSET `tracking:due` (score = `next_check_at` epoch),
- claim matching due active items by id with lease (`SKIP LOCKED`),
- enqueue dispatch jobs to Redis stream,
- fall back to Postgres due-claim about every `SCHEDULER_RECONCILE_POLL_SECONDS` for orphans,
- release claimed leases if queue write fails.

Idle cycles do not INFO-log `claimed=0`; use DEBUG if you need empty-cycle visibility.

Manual verification checklist: [docs/manual-test-redis-due-index.md](docs/manual-test-redis-due-index.md).

Execution semantics:
- at-least-once enqueue intent,
- worker processing is decoupled from dispatcher wake cadence.

## Scheduler Worker (`python -m app.jobs.scheduler_worker`)

Responsibilities:
- claim dispatch jobs from Redis stream consumer group,
- process claimed tracking checks via scheduler service,
- acknowledge stream entries on success/stale skip,
- reclaim stale pending jobs via idle-time auto-claim.

Recovery notes:
- If Redis loses the stream or consumer group while workers stay up, the worker recreates the group on `NOGROUP` and retries once.
- Manual unblock command:
  `redis-cli XGROUP CREATE tracking:scheduler:jobs tracking-scheduler-workers 0-0 MKSTREAM`
- Redis host warning fix:
  `sysctl vm.overcommit_memory=1`
  and persist `vm.overcommit_memory = 1` in `/etc/sysctl.conf`.

## Notifier (`python -m app.jobs.notifier`)

Responsibilities:
- wake on Redis stream `tracking:notifications` for ASAP first delivery,
- claim matching `outbound_notifications` rows by id (lease + `SKIP LOCKED`),
- fall back to Postgres due-claim about every `NOTIFIER_FALLBACK_POLL_SECONDS` for orphans/retries,
- send through sender adapter with bounded parallelism,
- finalize as `sent`, retry (`failed` + backoff), or `dead`,
- apply Telegram send pacing and honor Telegram `retry_after` backpressure hints.

Idle cycles do not INFO-log `claimed=0`; use DEBUG if you need empty-cycle visibility.

Manual verification checklist: [docs/manual-test-redis-notifier.md](docs/manual-test-redis-notifier.md).

Current senders:
- `TelegramNotificationSender` for connected Telegram chats.
- `LoggingNotificationSender` fallback for non-Telegram notification events.

## Data Model (Tracking Domain)

Primary tables:
- `tracked_items`
- `price_snapshots`
- `tracking_check_runs`
- `tracking_bootstrap_idempotency`
- `alert_events`
- `notification_events`

High-level relationships:
- one `tracked_item` -> many `price_snapshots`
- one `tracked_item` -> many `tracking_check_runs`
- one `alert_event` -> one/many `notification_events`

## Configuration

## Core API and scrape settings

| Variable | Default | Description |
|---|---:|---|
| `API_PREFIX` | `/api/v1` | versioned API prefix |
| `CORS_ALLOW_ORIGINS` | `*` | CORS list (`*` or comma-separated) |
| `SCRAPE_TIMEOUT_DEFAULT` | `30` | default timeout seconds |
| `SCRAPE_TIMEOUT_MIN` | `5` | min timeout accepted by API |
| `SCRAPE_TIMEOUT_MAX` | `120` | max timeout accepted by API |
| `SCRAPE_MAX_URLS` | `50` | max URLs per `/scrapes` or `/discover` request |
| `SCRAPE_CONCURRENCY` | `5` | scrape service worker concurrency |
| `SCRAPE_RESULT_CACHE_TTL_SECONDS` | `900` | TTL for `scrape_ref/result_ref` cache |
| `SCRAPE_RESULT_CACHE_MAX_BATCHES` | `500` | max cached scrape batches |
| `SCRAPE_PROXY_URLS` | empty | comma-separated outbound proxy URLs for Scrapling fetchers |
| `SCRAPE_PROXY_LIST_URLS` | empty | comma-separated text proxy-list URLs; one proxy URL per line |

## Tracking DB settings

| Variable | Default | Description |
|---|---:|---|
| `TRACKING_DATABASE_URL` | local postgres URL | primary DB URL |
| `SUPABASE_DB_URL` | empty | fallback alias |
| `DATABASE_URL` | empty | fallback alias |
| `TRACKING_DB_POOL_MIN_SIZE` | `1` | psycopg pool minimum |
| `TRACKING_DB_POOL_MAX_SIZE` | `10` | psycopg pool maximum |

## Tracking behavior settings

| Variable | Default | Description |
|---|---:|---|
| `TRACKING_INTERVAL_DEFAULT_MINUTES` | `60` | default check interval |
| `TRACKING_INTERVAL_MIN_MINUTES` | `5` | min allowed interval |
| `TRACKING_INTERVAL_MAX_MINUTES` | `10080` | max allowed interval (7 days) |
| `TRACKING_SNAPSHOT_DEFAULT_LIMIT` | `20` | default detail snapshot limit |

## Scheduler settings

| Variable | Default | Description |
|---|---:|---|
| `SCHEDULER_POLL_SECONDS` | `10` | legacy/compat setting (due-index dispatcher no longer sleeps on this) |
| `SCHEDULER_CLAIM_BATCH_SIZE` | `250` | due items claimed per cycle |
| `SCHEDULER_MAX_INFLIGHT` | `64` | max concurrent scrape checks |
| `SCHEDULER_LEASE_SECONDS` | `180` | claim lease TTL |
| `SCHEDULER_JITTER_PERCENT` | `10` | next-check jitter |
| `SCHEDULER_ALIGN_CHECKS_TO_INTERVAL` | `false` | align same-interval checks to shared UTC boundaries |
| `SCHEDULER_ALERT_COOLDOWN_MINUTES` | `60` | min gap between repeated alerts per rule |
| `SCHEDULER_LEASE_OWNER` | auto-generated | optional explicit lease owner id |
| `SCHEDULER_QUEUE_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis URL used by dispatcher/worker queue + due index |
| `SCHEDULER_QUEUE_STREAM_KEY` | `tracking:scheduler:jobs` | Redis stream key for scheduler dispatch jobs |
| `SCHEDULER_QUEUE_CONSUMER_GROUP` | `tracking-scheduler-workers` | Redis consumer group name |
| `SCHEDULER_QUEUE_MAX_STREAM_LENGTH` | `1000000` | approximate max stream length trimming target |
| `SCHEDULER_DUE_ZSET_KEY` | `tracking:due` | Redis ZSET wake index for due items |
| `SCHEDULER_DUE_BLOCK_SECONDS` | `5` | max sleep while waiting for next due score |
| `SCHEDULER_RECONCILE_POLL_SECONDS` | `60` | Postgres orphan/retry claim interval |
| `SCHEDULER_WORKER_CLAIM_BATCH_SIZE` | `64` | max dispatch jobs claimed per worker cycle |
| `SCHEDULER_WORKER_BLOCK_SECONDS` | `5` | blocking wait for new stream messages |
| `SCHEDULER_WORKER_PENDING_IDLE_SECONDS` | `60` | min idle seconds before auto-claiming pending jobs |

## Notifier settings

| Variable | Default | Description |
|---|---:|---|
| `NOTIFIER_POLL_SECONDS` | `5` | legacy/compat setting (stream-first notifier no longer sleeps on this) |
| `NOTIFIER_CLAIM_BATCH_SIZE` | `100` | notifications claimed per cycle |
| `NOTIFIER_LEASE_SECONDS` | `120` | notification lease TTL |
| `NOTIFIER_MAX_ATTEMPTS` | `5` | max delivery attempts |
| `NOTIFIER_BASE_BACKOFF_SECONDS` | `30` | exponential backoff base |
| `NOTIFIER_BACKOFF_MAX_SECONDS` | `3600` | max retry delay |
| `NOTIFIER_RETRY_JITTER_PERCENT` | `10` | retry jitter |
| `NOTIFIER_MAX_INFLIGHT` | `16` | max parallel sends per notifier cycle |
| `NOTIFIER_QUEUE_STREAM_KEY` | `tracking:notifications` | Redis wake stream |
| `NOTIFIER_QUEUE_CONSUMER_GROUP` | `tracking-notifier-workers` | Redis consumer group |
| `NOTIFIER_QUEUE_MAX_STREAM_LENGTH` | `1000000` | approximate stream maxlen |
| `NOTIFIER_WORKER_BLOCK_SECONDS` | `5` | XREADGROUP block when idle |
| `NOTIFIER_WORKER_PENDING_IDLE_SECONDS` | `60` | XAUTOCLAIM min idle |
| `NOTIFIER_FALLBACK_POLL_SECONDS` | `60` | Postgres orphan/retry claim interval |
| `TELEGRAM_SENDER_MIN_INTERVAL_MS` | `50` | per-worker minimum delay between Telegram API sends |
| `NOTIFIER_LEASE_OWNER` | auto-generated | optional explicit lease owner id |

## Notifier Sharding

Use simple process-level sharding for scale:
- Run multiple notifier processes (`python -m app.jobs.notifier`) on different hosts/containers.
- Set a distinct `NOTIFIER_LEASE_OWNER` / `NOTIFIER_WORKER_CONSUMER_NAME` per process for clearer logs/forensics.
- Keep one shared Redis stream consumer group + Postgres DB; leases + `SKIP LOCKED` distribute work.

## Operational Notes and Edge Cases

### `scrape_result_ref` is in-memory and short-lived
- Cache is process-local and TTL-bound.
- After API process restart or TTL expiry, refs become invalid.
- UI should track soon after scrape if using this optimization.

### Discovery may return broad results
- Heuristics can include pages user may not consider relevant.
- For strict single-item behavior use `max_depth=0`.

### Tracking bootstrap can update existing tracked item
- If same `source_url` already tracked, bootstrap refreshes existing item and appends a new snapshot.
- UI should not assume every bootstrap creates a new `item_id`.

### Monthly credits are enforced by scheduler
- Scheduler consumes `1` credit for each scheduled re-check attempt.
- When credits are exhausted (`overage_policy = block`), checks are deferred to the next billing period boundary.
- Credits are tracked per user in `usage_counters`.

### Target amount UX
- Use explicit monitor/alert control in UI.
- Send `target_amount: null` for monitor mode.
- In monitor mode, explain that the user will be notified whenever the confirmed product price changes.
- Do not send placeholder string values like `"string"`.

### Status transitions
- `active`: scheduler can claim/check item.
- `paused`: scheduler skips item until resumed.

### Idempotency best practice
- For create-track action, send `Idempotency-Key` from UI client.
- Reuse same key only for retried identical request payload.

## Current Limitations

- No auth: all endpoints are open in current MVP state.
- Notification sender is log-only (no email/SMS provider yet).
- No dedicated REST endpoints yet for alert/notification history exposure.
- Redis-backed scheduler queue split is available, but monolithic scheduler path is still supported for compatibility.

---

Additional docs:
- Architecture notes: `docs/backend-architecture-delivery.md`
- Postgres migration runbook: `docs/postgres-migration-runbook.md`

## Telegram Integration (New)

This project now includes a Telegram connect flow and Telegram delivery channel.

### New endpoints

- `POST /api/v1/integrations/telegram/connect`
  - creates a short-lived connect session
  - returns `deep_link_url` like `https://t.me/<bot>?start=<state>`
- `POST /api/v1/integrations/telegram/setup-webhook`
  - configures Telegram webhook via backend using `TELEGRAM_BOT_TOKEN`
  - input: `{ "public_base_url": "https://your-domain" }`
- `GET /api/v1/integrations/telegram/connect/{state}`
  - poll connection status (`pending`, `connected`, `expired`)
- `GET /api/v1/integrations/telegram/status`
  - returns currently active Telegram chat status used by notifier
- `POST /api/v1/integrations/telegram/webhook`
  - Telegram webhook receiver
  - validates `X-Telegram-Bot-Api-Secret-Token` when `TELEGRAM_WEBHOOK_SECRET` is configured

### End-to-end setup steps

1. Create a bot with `@BotFather`.
2. Save bot credentials in `.env`:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_BOT_USERNAME` (without `@`)
   - optional: `TELEGRAM_WEBHOOK_SECRET`
   - optional: `AFFILIATE_LINK_TEMPLATE`
     - empty: send direct product URL
     - contains `{url}`: uses encoded product URL replacement
     - otherwise appends `?url=<encoded>` (or `&url=`) to the template base URL
3. Expose your backend publicly (HTTPS) and set webhook through your API:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/integrations/telegram/setup-webhook \
  -H "Content-Type: application/json" \
  -d '{"public_base_url":"https://<your-domain>"}'
```

4. In UI, call `POST /api/v1/integrations/telegram/connect` and open `deep_link_url`.
5. User taps Start in Telegram bot chat (`/start <state>`).
6. Webhook processes update and marks session `connected`; active chat is stored.
7. Scheduler emits notification events with channel `telegram` when active chat exists.
8. Notifier sends via Telegram Bot API (`sendMessage`).

### Worker behavior with Telegram

- Scheduler uses `telegram` channel only when an active chat is connected.
- Otherwise it falls back to `log` channel.
- Notifier routes by channel:
  - `telegram` -> Telegram Bot API
  - other channels -> log sender
