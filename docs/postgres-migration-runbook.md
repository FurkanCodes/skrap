# SQLite -> Postgres Migration Runbook

Last updated: 2026-04-17
Owner: Backend
Applies to: `app.jobs.migrate_sqlite_to_postgres`

## Goal

Migrate tracking state from local SQLite to Postgres/Supabase with validation and clear rollback.

## Preconditions

- API + scheduler are stopped (no concurrent writers during migration).
- Source SQLite file exists (default: `./data/tracking.sqlite3`).
- Target Postgres is reachable.
- `.env` contains `TRACKING_DATABASE_URL`.

Recommended URL usage:

- App runtime: pooler URL (`:6543` on Supabase transaction pooler).
- Migration job: direct connection URL (`:5432`) when available for large bulk writes.

## 1) Backup

### Backup SQLite

```bash
cp ./data/tracking.sqlite3 ./data/tracking.sqlite3.backup.$(date +%Y%m%d%H%M%S)
```

### Backup Postgres target (before truncate)

```bash
pg_dump "$TRACKING_DATABASE_URL" \
  --data-only \
  --table=tracked_items \
  --table=price_snapshots \
  --table=tracking_check_runs \
  --table=tracking_bootstrap_idempotency \
  --table=alert_events \
  --table=notification_events \
  > ./data/postgres_tracking_backup_$(date +%Y%m%d%H%M%S).sql
```

## 2) Dry run (no truncate)

```bash
./.venv/bin/python -m app.jobs.migrate_sqlite_to_postgres \
  --sqlite-path ./data/tracking.sqlite3
```

Check output:

- `source_counts ...`
- `target_counts ...`
- `migration_complete`

This path is safe to rerun; writes are idempotent by unique keys.

## 3) Full import (truncate + strict verification)

```bash
./.venv/bin/python -m app.jobs.migrate_sqlite_to_postgres \
  --sqlite-path ./data/tracking.sqlite3 \
  --truncate
```

Expected behavior:

- Truncates target tracking tables in FK-safe order.
- Reimports all rows from SQLite.
- Verifies source and target counts match.
- Fails if counts differ.

## 4) Data validation checklist

- Count parity by table (already enforced when `--truncate`):
  - `tracked_items`
  - `price_snapshots`
  - `tracking_check_runs`
  - `tracking_bootstrap_idempotency`
  - `alert_events`
  - `notification_events`
- Spot-check random tracked items:
  - latest `last_amount`, `next_check_at`
  - recent snapshots
  - recent check runs
- Smoke run:
  - `./.venv/bin/python -m app.jobs.scheduler`
  - `./.venv/bin/python -m app.jobs.notifier`

## 5) Rollback

Choose one rollback path:

### A) Restore Postgres backup (recommended after truncate migration)

```bash
psql "$TRACKING_DATABASE_URL" -f ./data/postgres_tracking_backup_<timestamp>.sql
```

### B) Re-seed from SQLite backup

```bash
cp ./data/tracking.sqlite3.backup.<timestamp> ./data/tracking.sqlite3
./.venv/bin/python -m app.jobs.migrate_sqlite_to_postgres \
  --sqlite-path ./data/tracking.sqlite3 \
  --truncate
```

## 6) Operational notes

- Migration semantics are transactional: each invocation runs in one DB transaction.
- Repeated executions are safe (`ON CONFLICT` semantics).
- If migration fails, inspect logs and rerun after fixing the root cause.
