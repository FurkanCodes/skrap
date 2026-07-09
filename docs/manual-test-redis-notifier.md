# Manual test: Redis-wake notifier

Use this checklist after deploying or running the `feat/redis-wake-notifier` changes locally.

## Prerequisites

1. Redis listening on `127.0.0.1:6379` (or the URL in `SCHEDULER_QUEUE_REDIS_URL`).
2. Backend env loaded (`.env.local` + `.env`) with a working `TRACKING_DATABASE_URL`.
3. Processes running from the repo root:

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
./.venv/bin/python -m app.jobs.scheduler_dispatcher
./.venv/bin/python -m app.jobs.scheduler_worker
./.venv/bin/python -m app.jobs.notifier
```

4. Optional: Telegram connected for a test user. Without Telegram, notifications use the `log` channel and appear in notifier logs.

## 1. Idle quiet check

With an empty outbox, watch the notifier terminal for ~30 seconds.

- Expect: startup line `notifier_started ... mode=redis_wake`
- Expect: **no** repeating INFO lines with `claimed=0`
- Empty cycles stay at DEBUG only

## 2. ASAP path (happy path)

Trigger a real notification (preferred):

1. Ensure a tracked item is active for a user with Telegram connected (or accept `log`).
2. Cause a price-drop / alert / tracking-started event through the normal app flow.
3. Confirm delivery within a few seconds of enqueue (Telegram message or notifier INFO with `source=stream`).

Or force a wake through the app dual-write path by creating a tracker that emits `tracking_started`.

## 3. Redis stream visible

While a notification is enqueued/sent:

```bash
redis-cli XLEN tracking:notifications
redis-cli XINFO GROUPS tracking:notifications
```

Expect consumer group `tracking-notifier-workers` to exist.

## 4. Fallback / orphan recovery

Simulate a missed Redis wake (Postgres row without stream entry):

1. Insert a due queued row into `outbound_notifications` (valid `notification_id`, `status='queued'`, `next_attempt_at <= now()`, payload with `notification_type` the sender understands).
2. Do **not** `XADD` to Redis.
3. Wait up to ~60 seconds (`NOTIFIER_FALLBACK_POLL_SECONDS`).
4. Expect notifier INFO with `source=fallback` and the row moving to `sent` (or `failed`/`dead` if send cannot succeed).

## 5. Retry path

1. Temporarily break Telegram (invalid `TELEGRAM_BOT_TOKEN`) or use a payload that fails send.
2. Enqueue a telegram notification.
3. Expect status `failed` with a future `next_attempt_at`.
4. After backoff + fallback poll, expect another attempt; after max attempts, `dead`.

## 6. Ack / no poison

After a successful stream send:

- Notifier log shows `acked>=1` and `stale` only for missing/already-finalized IDs.
- Re-running the same stream entry should not double-send forever (stale/missing rows are ACKed).

```bash
redis-cli XPENDING tracking:notifications tracking-notifier-workers
```

Pending count should not grow unbounded while the notifier is healthy.

## 7. Regression

1. `GET /ready` returns healthy Postgres + Redis.
2. Dispatcher/worker still claim and process due tracked items.
3. API scrape/track endpoints still work.

## Knobs

| Env | Default | Role |
|---|---|---|
| `NOTIFIER_QUEUE_STREAM_KEY` | `tracking:notifications` | Wake stream |
| `NOTIFIER_QUEUE_CONSUMER_GROUP` | `tracking-notifier-workers` | Consumer group |
| `NOTIFIER_WORKER_BLOCK_SECONDS` | `5` | Redis block when idle |
| `NOTIFIER_FALLBACK_POLL_SECONDS` | `60` | Orphan/retry Postgres claim interval |
| `SCHEDULER_QUEUE_REDIS_URL` | `redis://127.0.0.1:6379/0` | Shared Redis |
