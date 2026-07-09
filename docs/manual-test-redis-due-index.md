# Manual test: Redis due-index dispatcher (Phase 2)

Use this after the Redis-wake notifier checklist in
[manual-test-redis-notifier.md](manual-test-redis-notifier.md).

## Prerequisites

1. Redis on `127.0.0.1:6379` (or `SCHEDULER_QUEUE_REDIS_URL`).
2. Restart **all** backend processes so they pick up due-index dual-write:

```bash
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
./.venv/bin/python -m app.jobs.scheduler_dispatcher
./.venv/bin/python -m app.jobs.scheduler_worker
./.venv/bin/python -m app.jobs.notifier
```

3. Expect dispatcher startup: `scheduler_dispatcher_started mode=redis_due_index`

## 1. Idle quiet check

With no due items (or empty `tracking:due`):

- No repeating INFO `claimed=0` every second
- Idle cycles stay at DEBUG (`source=idle`)
- Postgres claim only on reconcile (~every `SCHEDULER_RECONCILE_POLL_SECONDS`, default 60)

## 2. Due index populated on bootstrap / update

Create or update an active tracked item via API.

```bash
redis-cli ZCARD tracking:due
redis-cli ZRANGE tracking:due 0 -1 WITHSCORES
```

Expect the item id present with score ≈ `next_check_at` unix epoch.

Pause or delete the item → member removed from the ZSET.

## 3. ASAP dispatch when due

Force an item due soon (short interval / set `next_check_at` in the past via app update path, or wait).

Expect:

- Dispatcher INFO with `source=due_index claimed>=1 enqueued>=1`
- Worker processes the job
- After claim, item is removed from `tracking:due` until the worker reschedules `next_check_at`

## 4. Reconcile / orphan recovery

1. Insert/ensure an active due row in Postgres **without** a ZSET member (delete member manually):

```bash
redis-cli ZREM tracking:due <item_id>
```

2. Wait ≤ ~60s.
3. Expect dispatcher `source=fallback` and the item claimed/enqueued.

## 5. Regression

1. `GET /ready` healthy.
2. Notifier still quiet when idle; alerts still ASAP via `tracking:notifications`.
3. No 1Hz empty dispatcher INFO spam.

## Knobs

| Env | Default | Role |
|---|---|---|
| `SCHEDULER_DUE_ZSET_KEY` | `tracking:due` | Due wake index |
| `SCHEDULER_DUE_BLOCK_SECONDS` | `5` | Max sleep when waiting for next due |
| `SCHEDULER_RECONCILE_POLL_SECONDS` | `60` | Postgres orphan claim interval |
| `SCHEDULER_QUEUE_REDIS_URL` | `redis://127.0.0.1:6379/0` | Shared Redis |
