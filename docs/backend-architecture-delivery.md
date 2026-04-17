# Backend Architecture Through Delivery Framework

Last updated: 2026-04-17
Owner: Backend
Status: Draft (implementation guide)

## Scope

This document applies the Delivery Framework to the current price-tracking backend.

Goals:
- Keep existing REST API behavior stable.
- Support reliable periodic checks at 100k checks/day target.
- Prepare clean migration from SQLite single-node runtime to Redis + Postgres.

Out of scope (separate docs/workstreams):
- Authentication and authorization UX flows.
- Frontend product design.
- Vendor-specific notification providers.

## 1) Requirements

### Functional Requirements

Current:
- Scrape one or more URLs (`/api/v1/scrapes`).
- Discover related candidate URLs (`/api/v1/discover`).
- Bootstrap tracked item from URL (`POST /api/v1/tracked-items`).
- Read/update/delete tracked item.
- Periodic recheck scheduler writes snapshots and updates latest state.

Next:
- Track item with or without alert threshold.
- Per-user tracking ownership and subscriptions.
- Alert rule evaluation on each successful check.
- Notification dispatch and retry.

### Non-Functional Requirements

- API latency: P95 < 300ms for read/update endpoints (`/tracked-items*`), excluding scraping endpoints.
- Scheduler due-lag SLO: P95 <= 5 minutes.
- Delivery semantics: at-least-once check execution with idempotent writes.
- Availability: API and scheduler run as separate processes.
- Observability: end-to-end run status, lag, failure classes, blocked ratio.

### Capacity Baseline

- 100,000 checks/day ~= 1.16 checks/second average.
- Design for bursty schedules (top-of-hour clustering), not just average throughput.

## 2) Core Entities

## Current entities (implemented)

### `tracked_items`

Single latest state per source URL plus scheduler lease metadata.

Important columns:
- identity: `item_id`, `source_url`, `canonical_url`, `product_id`
- normalized product fields: `product_name`, `brand`, `sku`, `image_url`, `currency`
- latest observation: `last_amount`, `last_raw_text`, `last_source`, `last_checked_at`
- tracking config: `status`, `check_interval_minutes`, `target_amount`
- scheduler state: `next_check_at`, `lease_owner`, `lease_until`, `last_check_started_at`, `last_check_finished_at`

### `price_snapshots`

Append-only history of checks.

Important columns:
- `snapshot_id`, `item_id`, `checked_at`, `success`
- value payload: `amount`, `currency`, `raw_text`, `source`
- failure payload: `error`

### `tracking_check_runs`

Run-level audit and troubleshooting record.

Important columns:
- `run_id`, `item_id`, `scheduled_for`, `started_at`, `finished_at`
- `status` (`running|succeeded|failed`), `attempt`, `error`

## Proposed entities (next)

To avoid duplicate scraping when many users track the same URL, split "item state" from "user interest".

### `users` (future auth owner)

```sql
CREATE TABLE users (
  user_id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
```

### `tracked_item_subscriptions`

One user can subscribe to one tracked item with different modes/rules.

```sql
CREATE TABLE tracked_item_subscriptions (
  subscription_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('monitor','alert')),
  status TEXT NOT NULL CHECK (status IN ('active','paused')),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (user_id, item_id),
  FOREIGN KEY (item_id) REFERENCES tracked_items(item_id)
);
```

### `alert_rules`

Alert settings tied to subscription.

```sql
CREATE TABLE alert_rules (
  rule_id TEXT PRIMARY KEY,
  subscription_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('price_lte','price_gte','percent_drop')),
  target_amount NUMERIC(18,2),
  target_currency TEXT,
  cooldown_seconds INTEGER NOT NULL DEFAULT 21600,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (subscription_id) REFERENCES tracked_item_subscriptions(subscription_id)
);
```

### `notification_events`

Outbox-like table for send attempts.

```sql
CREATE TABLE notification_events (
  event_id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','sending','sent','failed','dead')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ,
  provider TEXT,
  provider_message_id TEXT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  UNIQUE (rule_id, snapshot_id),
  FOREIGN KEY (rule_id) REFERENCES alert_rules(rule_id),
  FOREIGN KEY (item_id) REFERENCES tracked_items(item_id),
  FOREIGN KEY (snapshot_id) REFERENCES price_snapshots(snapshot_id)
);
```

## 3) API / Interface

### Existing endpoints (unchanged)

- `GET /health`
- `GET /api/v1/sites`
- `POST /api/v1/scrapes`
- `POST /api/v1/discover`
- `POST /api/v1/tracked-items`
- `GET /api/v1/tracked-items`
- `GET /api/v1/tracked-items/{item_id}`
- `PATCH /api/v1/tracked-items/{item_id}`
- `DELETE /api/v1/tracked-items/{item_id}`

### Proposed endpoint additions (next)

Alert lifecycle:
- `POST /api/v1/tracked-items/{item_id}/subscriptions`
- `GET /api/v1/tracked-items/{item_id}/subscriptions`
- `PATCH /api/v1/subscriptions/{subscription_id}`
- `POST /api/v1/subscriptions/{subscription_id}/rules`
- `PATCH /api/v1/rules/{rule_id}`
- `DELETE /api/v1/rules/{rule_id}`

Notification visibility:
- `GET /api/v1/notifications?status=queued|sent|failed`
- `POST /api/v1/notifications/{event_id}/retry`

### API behavior requirements

- Add idempotency support for bootstrap/create-subscription endpoints:
  - request header: `Idempotency-Key`
  - same key + same payload should return same logical object.
- Keep response ordering deterministic for batch scrape/discover results.

## 4) Dataflow

### A) Bootstrap Tracking

1. API receives URL and optional target.
2. Scrape pipeline resolves normalized `PriceResult`.
3. Upsert `tracked_items` row and append first `price_snapshots` row.
4. Set `next_check_at = now + check_interval_minutes`.

### B) Periodic Scheduler Check

1. Scheduler polls due rows (`status='active'` and `next_check_at <= now`).
2. Claims rows by setting lease (`lease_owner`, `lease_until`).
3. Inserts `tracking_check_runs` (`status='running'`).
4. Executes batched scrape using existing scrape pipeline.
5. Writes snapshot and updates latest item state atomically.
6. Marks run as succeeded/failed.
7. Advances `next_check_at` with jitter.

### C) Alert Evaluation (next)

1. On successful snapshot, load active subscriptions/rules for item.
2. Evaluate crossing logic (example: previously above target, now below target).
3. Create `notification_events` rows (idempotent by `UNIQUE(rule_id, snapshot_id)`).

### D) Notification Dispatch (next)

1. Notifier worker claims queued due events.
2. Sends via provider.
3. Marks `sent` or schedules retry with exponential backoff.
4. Moves to `dead` after max attempts.

## 5) High-Level Design

### Phase 1 (current)

- FastAPI process.
- Separate scheduler process (`python -m app.jobs.scheduler`).
- SQLite as source of truth.
- Scrapling runtime for extraction.

Strengths:
- Simple operations.
- Fast iteration.

Limits:
- SQLite writer contention at scale.
- Single-node operational profile.

### Phase 2 (target)

- API nodes: stateless, horizontally scalable.
- Scheduler: computes due work and enqueues jobs.
- Redis queue: transient transport.
- Worker pool: executes scraping jobs.
- Postgres: durable system of record.
- Notifier workers: independent send/retry loop.

Compatibility rule:
- Keep application interfaces (`claim`, `run`, `persist`) stable so transport/storage adapters can be swapped without changing REST API contracts.

## 6) Deep Dives

### Consistency and Semantics

- Check execution: at-least-once.
- Persistence: ACID writes for tracked state and run/snapshot records.
- Alert emission: idempotent by unique event key (`rule_id + snapshot_id`).

### Scheduling and Backpressure

Current knobs:
- `SCHEDULER_POLL_SECONDS` (default 10)
- `SCHEDULER_CLAIM_BATCH_SIZE` (default 250)
- `SCHEDULER_MAX_INFLIGHT` (default 64)
- `SCHEDULER_LEASE_SECONDS` (default 180)
- `SCHEDULER_JITTER_PERCENT` (default 10)

Guidance:
- Keep lease > expected max scrape duration.
- Use jitter to reduce synchronized spikes.
- Keep inflight bounded to protect CPU/memory and target sites.

### Proxy and Retry Strategy

- Default path: fast HTTP fetch mode.
- Escalate to dynamic/stealth only when blocked/protection signals indicate need.
- Use proxy rotation primarily on blocked paths, not every request.

### Critical Edge Cases

- Product removed or URL changed:
  - record failed snapshot with meaningful error code.
  - do not overwrite last known good price.

- Currency mismatch:
  - normalize/convert before threshold comparison (rule currency required).

- Duplicate rows from retries:
  - expected with at-least-once; constrain side effects with run/event unique keys.

- Pause/delete during execution:
  - do not claim paused rows.
  - finalize safely if already claimed; skip future claims.

- Scheduler crash mid-run:
  - lease expiry makes item claimable again.
  - run row remains audit evidence for forensic checks.

### Observability and SLO Tracking

Track and alert on:
- scheduler due-lag (p50/p95/p99)
- run success/failure counts
- blocked ratio by site and fetch mode
- average scrape duration
- queue depth and worker lag (Phase 2)
- notification send success/failure and dead-letter counts

## Delivery Roadmap

### Milestone A: Hardening current scheduler

- Expose scheduler fields in tracked-item detail (`next_check_at`, lease/run timestamps).
- Add metrics endpoint/log counters for due-lag and failure classes.
- Add idempotency key store for bootstrap endpoint.

### Milestone B: Alerts and notifications

- Implement `tracked_item_subscriptions`, `alert_rules`, `notification_events`.
- Add alert evaluator and notifier worker.
- Add cooldown and crossing semantics.

### Milestone C: Infrastructure migration

- Move persistence from SQLite to Postgres.
- Add Redis queue and split scheduler enqueue from worker execute.
- Preserve REST contracts and application service interfaces.

## Suggested Linear Story Cuts

1. Add tracked item detail scheduler metadata in API responses.
2. Add scheduler metrics and due-lag instrumentation.
3. Add idempotency-key support for tracked-item bootstrap.
4. Implement subscription and alert-rule schema + migrations.
5. Build alert evaluator on snapshot write path.
6. Implement notification events outbox and retry worker.
7. Add Postgres repository adapter and migration scripts.
8. Add Redis dispatcher adapter and worker queue runtime.
9. Run load/soak validation for 100k checks/day target.

