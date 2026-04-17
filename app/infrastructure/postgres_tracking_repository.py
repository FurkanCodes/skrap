from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.tracking.domain import (
    AlertEvent,
    NotificationEvent,
    NotificationEventStatus,
    PriceSnapshot,
    TrackedItem,
    TrackingBootstrapIdempotencyRecord,
    TrackingCheckRun,
    TrackingCheckRunStatus,
)

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracked_items (
        item_id TEXT PRIMARY KEY,
        source_url TEXT NOT NULL UNIQUE,
        canonical_url TEXT,
        product_id TEXT,
        site_key TEXT,
        adapter_key TEXT,
        product_name TEXT,
        brand TEXT,
        model TEXT,
        sku TEXT,
        gtin TEXT,
        image_url TEXT,
        availability TEXT,
        currency TEXT,
        last_amount TEXT,
        last_raw_text TEXT,
        last_source TEXT,
        status TEXT NOT NULL CHECK (status IN ('active', 'paused')),
        check_interval_minutes INTEGER NOT NULL CHECK (check_interval_minutes >= 1),
        target_amount TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        last_checked_at TIMESTAMPTZ,
        next_check_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_until TIMESTAMPTZ,
        last_check_started_at TIMESTAMPTZ,
        last_check_finished_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES tracked_items(item_id) ON DELETE CASCADE,
        checked_at TIMESTAMPTZ NOT NULL,
        scheduled_for TIMESTAMPTZ,
        success BOOLEAN NOT NULL,
        amount TEXT,
        currency TEXT,
        raw_text TEXT,
        source TEXT,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracking_check_runs (
        run_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES tracked_items(item_id) ON DELETE CASCADE,
        scheduled_for TIMESTAMPTZ NOT NULL,
        job_key TEXT,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
        attempt INTEGER NOT NULL CHECK (attempt >= 1),
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_events (
        event_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        item_id TEXT NOT NULL REFERENCES tracked_items(item_id) ON DELETE CASCADE,
        snapshot_id TEXT NOT NULL REFERENCES price_snapshots(snapshot_id) ON DELETE CASCADE,
        rule_type TEXT NOT NULL CHECK (rule_type IN ('price_below')),
        previous_amount TEXT,
        current_amount TEXT NOT NULL,
        target_amount TEXT NOT NULL,
        currency TEXT,
        triggered_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracking_bootstrap_idempotency (
        idempotency_key TEXT PRIMARY KEY,
        request_hash TEXT NOT NULL,
        item_id TEXT REFERENCES tracked_items(item_id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_events (
        notification_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        alert_event_id TEXT NOT NULL REFERENCES alert_events(event_id) ON DELETE CASCADE,
        item_id TEXT NOT NULL REFERENCES tracked_items(item_id) ON DELETE CASCADE,
        channel TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'dead')),
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
        next_attempt_at TIMESTAMPTZ NOT NULL,
        last_attempt_at TIMESTAMPTZ,
        sent_at TIMESTAMPTZ,
        provider_name TEXT,
        provider_message_id TEXT,
        last_error TEXT,
        lease_owner TEXT,
        lease_until TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tracked_items_due_active
      ON tracked_items(next_check_at, item_id)
      WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tracked_items_lease_until
      ON tracked_items(lease_until)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tracked_items_status_next_lease
      ON tracked_items(status, next_check_at, lease_until)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_price_snapshots_item_checked
      ON price_snapshots(item_id, checked_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_price_snapshots_item_schedule_unique
      ON price_snapshots(item_id, scheduled_for)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_check_runs_item_started
      ON tracking_check_runs(item_id, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_check_runs_item_scheduled
      ON tracking_check_runs(item_id, scheduled_for, attempt DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_check_runs_job_key_unique
      ON tracking_check_runs(job_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bootstrap_idempotency_item
      ON tracking_bootstrap_idempotency(item_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_alert_events_item_triggered
      ON alert_events(item_id, triggered_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_alert_events_rule_triggered
      ON alert_events(rule_type, triggered_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_events_due
      ON notification_events(status, next_attempt_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_events_lease_until
      ON notification_events(lease_until)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_events_item_created
      ON notification_events(item_id, created_at DESC)
    """,
)


class PostgresTrackingRepository:
    """PostgreSQL-backed persistence for tracked items and scheduler state."""

    def __init__(
        self,
        database_url: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
    ) -> None:
        if min_pool_size < 1:
            raise ValueError("min_pool_size must be >= 1")
        if max_pool_size < min_pool_size:
            raise ValueError("max_pool_size must be >= min_pool_size")

        self._lock = RLock()
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            # Supabase PgBouncer (transaction pooler) is not compatible with
            # psycopg's auto-prepared statements. Disabling auto-prepare
            # prevents intermittent `DuplicatePreparedStatement` errors.
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
            },
            open=False,
        )

    def initialize(self) -> None:
        """Create schema/indexes and open connection pool."""
        with self._lock:
            self._pool.open(wait=True)
            with self._pool.connection() as conn:
                with conn.transaction():
                    for statement in _SCHEMA_STATEMENTS:
                        conn.execute(statement)

    def close(self) -> None:
        """Close pooled connections (useful in tests/shutdown hooks)."""
        with self._lock:
            self._pool.close()

    def list_items(self) -> list[TrackedItem]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tracked_items
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def get_item(self, item_id: str) -> TrackedItem | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE item_id = %s
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def get_item_by_source_url(self, source_url: str) -> TrackedItem | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE source_url = %s
                """,
                (source_url,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def save_item(self, item: TrackedItem) -> TrackedItem:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO tracked_items (
                    item_id,
                    source_url,
                    canonical_url,
                    product_id,
                    site_key,
                    adapter_key,
                    product_name,
                    brand,
                    model,
                    sku,
                    gtin,
                    image_url,
                    availability,
                    currency,
                    last_amount,
                    last_raw_text,
                    last_source,
                    status,
                    check_interval_minutes,
                    target_amount,
                    created_at,
                    updated_at,
                    last_checked_at,
                    next_check_at,
                    lease_owner,
                    lease_until,
                    last_check_started_at,
                    last_check_finished_at
                ) VALUES (
                    %(item_id)s,
                    %(source_url)s,
                    %(canonical_url)s,
                    %(product_id)s,
                    %(site_key)s,
                    %(adapter_key)s,
                    %(product_name)s,
                    %(brand)s,
                    %(model)s,
                    %(sku)s,
                    %(gtin)s,
                    %(image_url)s,
                    %(availability)s,
                    %(currency)s,
                    %(last_amount)s,
                    %(last_raw_text)s,
                    %(last_source)s,
                    %(status)s,
                    %(check_interval_minutes)s,
                    %(target_amount)s,
                    %(created_at)s,
                    %(updated_at)s,
                    %(last_checked_at)s,
                    %(next_check_at)s,
                    %(lease_owner)s,
                    %(lease_until)s,
                    %(last_check_started_at)s,
                    %(last_check_finished_at)s
                )
                ON CONFLICT(source_url) DO UPDATE SET
                    canonical_url = EXCLUDED.canonical_url,
                    product_id = EXCLUDED.product_id,
                    site_key = EXCLUDED.site_key,
                    adapter_key = EXCLUDED.adapter_key,
                    product_name = EXCLUDED.product_name,
                    brand = EXCLUDED.brand,
                    model = EXCLUDED.model,
                    sku = EXCLUDED.sku,
                    gtin = EXCLUDED.gtin,
                    image_url = EXCLUDED.image_url,
                    availability = EXCLUDED.availability,
                    currency = EXCLUDED.currency,
                    last_amount = EXCLUDED.last_amount,
                    last_raw_text = EXCLUDED.last_raw_text,
                    last_source = EXCLUDED.last_source,
                    status = EXCLUDED.status,
                    check_interval_minutes = EXCLUDED.check_interval_minutes,
                    target_amount = EXCLUDED.target_amount,
                    updated_at = EXCLUDED.updated_at,
                    last_checked_at = EXCLUDED.last_checked_at,
                    next_check_at = EXCLUDED.next_check_at,
                    lease_owner = EXCLUDED.lease_owner,
                    lease_until = EXCLUDED.lease_until,
                    last_check_started_at = EXCLUDED.last_check_started_at,
                    last_check_finished_at = EXCLUDED.last_check_finished_at
                RETURNING *
                """,
                self._item_params(item),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to persist tracked item")
        return self._row_to_item(row)

    def save_item_and_snapshot(self, item: TrackedItem, snapshot: PriceSnapshot) -> TrackedItem:
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO tracked_items (
                        item_id,
                        source_url,
                        canonical_url,
                        product_id,
                        site_key,
                        adapter_key,
                        product_name,
                        brand,
                        model,
                        sku,
                        gtin,
                        image_url,
                        availability,
                        currency,
                        last_amount,
                        last_raw_text,
                        last_source,
                        status,
                        check_interval_minutes,
                        target_amount,
                        created_at,
                        updated_at,
                        last_checked_at,
                        next_check_at,
                        lease_owner,
                        lease_until,
                        last_check_started_at,
                        last_check_finished_at
                    ) VALUES (
                        %(item_id)s,
                        %(source_url)s,
                        %(canonical_url)s,
                        %(product_id)s,
                        %(site_key)s,
                        %(adapter_key)s,
                        %(product_name)s,
                        %(brand)s,
                        %(model)s,
                        %(sku)s,
                        %(gtin)s,
                        %(image_url)s,
                        %(availability)s,
                        %(currency)s,
                        %(last_amount)s,
                        %(last_raw_text)s,
                        %(last_source)s,
                        %(status)s,
                        %(check_interval_minutes)s,
                        %(target_amount)s,
                        %(created_at)s,
                        %(updated_at)s,
                        %(last_checked_at)s,
                        %(next_check_at)s,
                        %(lease_owner)s,
                        %(lease_until)s,
                        %(last_check_started_at)s,
                        %(last_check_finished_at)s
                    )
                    ON CONFLICT(source_url) DO UPDATE SET
                        canonical_url = EXCLUDED.canonical_url,
                        product_id = EXCLUDED.product_id,
                        site_key = EXCLUDED.site_key,
                        adapter_key = EXCLUDED.adapter_key,
                        product_name = EXCLUDED.product_name,
                        brand = EXCLUDED.brand,
                        model = EXCLUDED.model,
                        sku = EXCLUDED.sku,
                        gtin = EXCLUDED.gtin,
                        image_url = EXCLUDED.image_url,
                        availability = EXCLUDED.availability,
                        currency = EXCLUDED.currency,
                        last_amount = EXCLUDED.last_amount,
                        last_raw_text = EXCLUDED.last_raw_text,
                        last_source = EXCLUDED.last_source,
                        status = EXCLUDED.status,
                        check_interval_minutes = EXCLUDED.check_interval_minutes,
                        target_amount = EXCLUDED.target_amount,
                        updated_at = EXCLUDED.updated_at,
                        last_checked_at = EXCLUDED.last_checked_at,
                        next_check_at = EXCLUDED.next_check_at,
                        lease_owner = EXCLUDED.lease_owner,
                        lease_until = EXCLUDED.lease_until,
                        last_check_started_at = EXCLUDED.last_check_started_at,
                        last_check_finished_at = EXCLUDED.last_check_finished_at
                    RETURNING *
                    """,
                    self._item_params(item),
                ).fetchone()
                self._insert_snapshot(conn, snapshot)

        if row is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to persist tracking check result")
        return self._row_to_item(row)

    def delete_item(self, item_id: str) -> bool:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM tracked_items
                WHERE item_id = %s
                """,
                (item_id,),
            )
        return cursor.rowcount > 0

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        with self._pool.connection() as conn:
            self._insert_snapshot(conn, snapshot)

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM price_snapshots
                WHERE item_id = %s
                ORDER BY checked_at DESC
                LIMIT %s
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    def create_alert_event(self, event: AlertEvent) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO alert_events (
                    event_id,
                    idempotency_key,
                    item_id,
                    snapshot_id,
                    rule_type,
                    previous_amount,
                    current_amount,
                    target_amount,
                    currency,
                    triggered_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(idempotency_key) DO NOTHING
                RETURNING event_id
                """,
                (
                    event.event_id,
                    event.idempotency_key,
                    event.item_id,
                    event.snapshot_id,
                    event.rule_type,
                    event.previous_amount,
                    event.current_amount,
                    event.target_amount,
                    event.currency,
                    _parse_timestamp(event.triggered_at),
                ),
            ).fetchone()
        return row is not None

    def get_last_alert_event(
        self,
        *,
        item_id: str,
        rule_type: str,
    ) -> AlertEvent | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM alert_events
                WHERE item_id = %s
                  AND rule_type = %s
                ORDER BY triggered_at DESC
                LIMIT 1
                """,
                (item_id, rule_type),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_alert_event(row)

    def list_alert_events(self, item_id: str, limit: int) -> list[AlertEvent]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM alert_events
                WHERE item_id = %s
                ORDER BY triggered_at DESC
                LIMIT %s
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_alert_event(row) for row in rows]

    def enqueue_notification_event(self, event: NotificationEvent) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO notification_events (
                    notification_id,
                    idempotency_key,
                    alert_event_id,
                    item_id,
                    channel,
                    payload,
                    status,
                    attempt_count,
                    max_attempts,
                    next_attempt_at,
                    last_attempt_at,
                    sent_at,
                    provider_name,
                    provider_message_id,
                    last_error,
                    lease_owner,
                    lease_until,
                    created_at,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT(idempotency_key) DO NOTHING
                RETURNING notification_id
                """,
                (
                    event.notification_id,
                    event.idempotency_key,
                    event.alert_event_id,
                    event.item_id,
                    event.channel,
                    event.payload,
                    event.status,
                    event.attempt_count,
                    event.max_attempts,
                    _parse_timestamp(event.next_attempt_at),
                    _parse_timestamp(event.last_attempt_at),
                    _parse_timestamp(event.sent_at),
                    event.provider_name,
                    event.provider_message_id,
                    event.last_error,
                    event.lease_owner,
                    _parse_timestamp(event.lease_until),
                    _parse_timestamp(event.created_at),
                    _parse_timestamp(event.updated_at),
                ),
            ).fetchone()
        return row is not None

    def claim_due_notification_events(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[NotificationEvent]:
        if limit < 1:
            return []

        now_dt = _parse_timestamp(now)
        lease_until_dt = _parse_timestamp(lease_until)
        with self._pool.connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT notification_id
                    FROM notification_events
                    WHERE status IN ('queued', 'failed', 'sending')
                      AND next_attempt_at <= %s
                      AND (lease_until IS NULL OR lease_until <= %s)
                      AND status != 'dead'
                      AND status != 'sent'
                    ORDER BY next_attempt_at ASC, notification_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (now_dt, now_dt, limit),
                ).fetchall()
                notification_ids = [str(row["notification_id"]) for row in rows]
                if not notification_ids:
                    return []

                conn.execute(
                    """
                    UPDATE notification_events
                    SET status = 'sending',
                        lease_owner = %s,
                        lease_until = %s,
                        last_attempt_at = %s,
                        updated_at = %s
                    WHERE notification_id = ANY(%s)
                    """,
                    (lease_owner, lease_until_dt, now_dt, now_dt, notification_ids),
                )

                claimed_rows = conn.execute(
                    """
                    SELECT *
                    FROM notification_events
                    WHERE notification_id = ANY(%s)
                    """,
                    (notification_ids,),
                ).fetchall()

        by_id = {
            str(row["notification_id"]): self._row_to_notification_event(row)
            for row in claimed_rows
        }
        return [
            by_id[notification_id]
            for notification_id in notification_ids
            if notification_id in by_id
        ]

    def finalize_notification_event(
        self,
        *,
        notification_id: str,
        status: NotificationEventStatus,
        attempt_count: int,
        now: str,
        next_attempt_at: str | None,
        sent_at: str | None,
        provider_name: str | None,
        provider_message_id: str | None,
        error: str | None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE notification_events
                SET status = %s,
                    attempt_count = %s,
                    next_attempt_at = %s,
                    sent_at = %s,
                    provider_name = %s,
                    provider_message_id = %s,
                    last_error = %s,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = %s
                WHERE notification_id = %s
                """,
                (
                    status,
                    attempt_count,
                    _parse_timestamp(next_attempt_at),
                    _parse_timestamp(sent_at),
                    provider_name,
                    provider_message_id,
                    error,
                    _parse_timestamp(now),
                    notification_id,
                ),
            )

    def list_notification_events(self, item_id: str, limit: int) -> list[NotificationEvent]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM notification_events
                WHERE item_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_notification_event(row) for row in rows]

    def claim_due_items(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[TrackedItem]:
        if limit < 1:
            return []

        now_dt = _parse_timestamp(now)
        lease_until_dt = _parse_timestamp(lease_until)

        with self._pool.connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT item_id
                    FROM tracked_items
                    WHERE status = 'active'
                      AND (next_check_at IS NULL OR next_check_at <= %s)
                      AND (lease_until IS NULL OR lease_until <= %s)
                    ORDER BY COALESCE(next_check_at, updated_at, created_at) ASC, item_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (now_dt, now_dt, limit),
                ).fetchall()
                item_ids = [str(row["item_id"]) for row in rows]
                if not item_ids:
                    return []

                conn.execute(
                    """
                    UPDATE tracked_items
                    SET lease_owner = %s,
                        lease_until = %s,
                        last_check_started_at = %s,
                        updated_at = %s
                    WHERE item_id = ANY(%s)
                    """,
                    (lease_owner, lease_until_dt, now_dt, now_dt, item_ids),
                )

                claimed_rows = conn.execute(
                    """
                    SELECT *
                    FROM tracked_items
                    WHERE item_id = ANY(%s)
                    """,
                    (item_ids,),
                ).fetchall()

        by_id = {str(row["item_id"]): self._row_to_item(row) for row in claimed_rows}
        return [by_id[item_id] for item_id in item_ids if item_id in by_id]

    def start_check_run(
        self,
        *,
        run_id: str,
        item_id: str,
        scheduled_for: str,
        job_key: str,
        started_at: str,
    ) -> TrackingCheckRun:
        scheduled_for_dt = _parse_timestamp(scheduled_for)
        started_at_dt = _parse_timestamp(started_at)

        with self._pool.connection() as conn:
            with conn.transaction():
                existing = conn.execute(
                    """
                    SELECT *
                    FROM tracking_check_runs
                    WHERE job_key = %s
                    LIMIT 1
                    """,
                    (job_key,),
                ).fetchone()
                if existing is not None:
                    return self._row_to_check_run(existing)

                attempt_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(attempt), 0) AS max_attempt
                    FROM tracking_check_runs
                    WHERE item_id = %s
                      AND scheduled_for = %s
                    """,
                    (item_id, scheduled_for_dt),
                ).fetchone()
                attempt = int((attempt_row["max_attempt"] if attempt_row is not None else 0) or 0) + 1

                inserted = conn.execute(
                    """
                    INSERT INTO tracking_check_runs (
                        run_id,
                        item_id,
                        scheduled_for,
                        job_key,
                        started_at,
                        finished_at,
                        status,
                        attempt,
                        error
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(job_key) DO NOTHING
                    RETURNING *
                    """,
                    (
                        run_id,
                        item_id,
                        scheduled_for_dt,
                        job_key,
                        started_at_dt,
                        None,
                        "running",
                        attempt,
                        None,
                    ),
                ).fetchone()

                if inserted is None:
                    existing = conn.execute(
                        """
                        SELECT *
                        FROM tracking_check_runs
                        WHERE job_key = %s
                        LIMIT 1
                        """,
                        (job_key,),
                    ).fetchone()
                    if existing is None:  # pragma: no cover - defensive guard
                        raise RuntimeError("Failed to start or load check run")
                    return self._row_to_check_run(existing)

        return self._row_to_check_run(inserted)

    def finish_check_run(
        self,
        *,
        run_id: str,
        status: TrackingCheckRunStatus,
        finished_at: str,
        error: str | None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE tracking_check_runs
                SET status = %s,
                    finished_at = %s,
                    error = %s
                WHERE run_id = %s
                """,
                (status, _parse_timestamp(finished_at), error, run_id),
            )

    def list_check_runs(self, item_id: str, limit: int) -> list[TrackingCheckRun]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tracking_check_runs
                WHERE item_id = %s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_check_run(row) for row in rows]

    def get_bootstrap_idempotency_record(
        self,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tracking_bootstrap_idempotency
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_bootstrap_idempotency(row)

    def save_bootstrap_idempotency_record(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        now_dt = _parse_timestamp(now)
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO tracking_bootstrap_idempotency (
                    idempotency_key,
                    request_hash,
                    item_id,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    request_hash = EXCLUDED.request_hash,
                    item_id = EXCLUDED.item_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (idempotency_key, request_hash, item_id, now_dt, now_dt),
            )

    def _insert_snapshot(self, conn, snapshot: PriceSnapshot) -> None:
        conn.execute(
            """
            INSERT INTO price_snapshots (
                snapshot_id,
                item_id,
                checked_at,
                scheduled_for,
                success,
                amount,
                currency,
                raw_text,
                source,
                error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(item_id, scheduled_for) DO UPDATE SET
                checked_at = EXCLUDED.checked_at,
                success = CASE
                    WHEN price_snapshots.success THEN TRUE
                    ELSE EXCLUDED.success
                END,
                amount = CASE
                    WHEN EXCLUDED.success OR NOT price_snapshots.success
                    THEN EXCLUDED.amount
                    ELSE price_snapshots.amount
                END,
                currency = CASE
                    WHEN EXCLUDED.success OR NOT price_snapshots.success
                    THEN EXCLUDED.currency
                    ELSE price_snapshots.currency
                END,
                raw_text = CASE
                    WHEN EXCLUDED.success OR NOT price_snapshots.success
                    THEN EXCLUDED.raw_text
                    ELSE price_snapshots.raw_text
                END,
                source = CASE
                    WHEN EXCLUDED.success OR NOT price_snapshots.success
                    THEN EXCLUDED.source
                    ELSE price_snapshots.source
                END,
                error = CASE
                    WHEN EXCLUDED.success THEN NULL
                    WHEN price_snapshots.success THEN price_snapshots.error
                    ELSE EXCLUDED.error
                END
            """,
            (
                snapshot.snapshot_id,
                snapshot.item_id,
                _parse_timestamp(snapshot.checked_at),
                _parse_timestamp(snapshot.scheduled_for),
                bool(snapshot.success),
                snapshot.amount,
                snapshot.currency,
                snapshot.raw_text,
                snapshot.source,
                snapshot.error,
            ),
        )

    @staticmethod
    def _item_params(item: TrackedItem) -> dict[str, object | None]:
        return {
            "item_id": item.item_id,
            "source_url": item.source_url,
            "canonical_url": item.canonical_url,
            "product_id": item.product_id,
            "site_key": item.site_key,
            "adapter_key": item.adapter_key,
            "product_name": item.product_name,
            "brand": item.brand,
            "model": item.model,
            "sku": item.sku,
            "gtin": item.gtin,
            "image_url": item.image_url,
            "availability": item.availability,
            "currency": item.currency,
            "last_amount": item.last_amount,
            "last_raw_text": item.last_raw_text,
            "last_source": item.last_source,
            "status": item.status,
            "check_interval_minutes": item.check_interval_minutes,
            "target_amount": item.target_amount,
            "created_at": _parse_timestamp(item.created_at),
            "updated_at": _parse_timestamp(item.updated_at),
            "last_checked_at": _parse_timestamp(item.last_checked_at),
            "next_check_at": _parse_timestamp(item.next_check_at),
            "lease_owner": item.lease_owner,
            "lease_until": _parse_timestamp(item.lease_until),
            "last_check_started_at": _parse_timestamp(item.last_check_started_at),
            "last_check_finished_at": _parse_timestamp(item.last_check_finished_at),
        }

    @staticmethod
    def _row_to_item(row: dict[str, object]) -> TrackedItem:
        return TrackedItem(
            item_id=str(row["item_id"]),
            source_url=str(row["source_url"]),
            canonical_url=_as_str(row.get("canonical_url")),
            product_id=_as_str(row.get("product_id")),
            site_key=_as_str(row.get("site_key")),
            adapter_key=_as_str(row.get("adapter_key")),
            product_name=_as_str(row.get("product_name")),
            brand=_as_str(row.get("brand")),
            model=_as_str(row.get("model")),
            sku=_as_str(row.get("sku")),
            gtin=_as_str(row.get("gtin")),
            image_url=_as_str(row.get("image_url")),
            availability=_as_str(row.get("availability")),
            currency=_as_str(row.get("currency")),
            last_amount=_as_str(row.get("last_amount")),
            last_raw_text=_as_str(row.get("last_raw_text")),
            last_source=_as_str(row.get("last_source")),
            status=str(row["status"]),
            check_interval_minutes=int(row["check_interval_minutes"]),
            target_amount=_as_str(row.get("target_amount")),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
            last_checked_at=_format_timestamp(row.get("last_checked_at")),
            next_check_at=_format_timestamp(row.get("next_check_at")),
            lease_owner=_as_str(row.get("lease_owner")),
            lease_until=_format_timestamp(row.get("lease_until")),
            last_check_started_at=_format_timestamp(row.get("last_check_started_at")),
            last_check_finished_at=_format_timestamp(row.get("last_check_finished_at")),
        )

    @staticmethod
    def _row_to_snapshot(row: dict[str, object]) -> PriceSnapshot:
        return PriceSnapshot(
            snapshot_id=str(row["snapshot_id"]),
            item_id=str(row["item_id"]),
            checked_at=_format_timestamp(row.get("checked_at")),
            success=bool(row["success"]),
            amount=_as_str(row.get("amount")),
            currency=_as_str(row.get("currency")),
            raw_text=_as_str(row.get("raw_text")),
            source=_as_str(row.get("source")),
            error=_as_str(row.get("error")),
            scheduled_for=_format_timestamp(row.get("scheduled_for")),
        )

    @staticmethod
    def _row_to_alert_event(row: dict[str, object]) -> AlertEvent:
        return AlertEvent(
            event_id=str(row["event_id"]),
            idempotency_key=str(row["idempotency_key"]),
            item_id=str(row["item_id"]),
            snapshot_id=str(row["snapshot_id"]),
            rule_type=str(row["rule_type"]),
            previous_amount=_as_str(row.get("previous_amount")),
            current_amount=str(row["current_amount"]),
            target_amount=str(row["target_amount"]),
            currency=_as_str(row.get("currency")),
            triggered_at=_format_timestamp(row.get("triggered_at")),
        )

    @staticmethod
    def _row_to_notification_event(row: dict[str, object]) -> NotificationEvent:
        return NotificationEvent(
            notification_id=str(row["notification_id"]),
            idempotency_key=str(row["idempotency_key"]),
            alert_event_id=str(row["alert_event_id"]),
            item_id=str(row["item_id"]),
            channel=str(row["channel"]),
            payload=str(row["payload"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_at=_format_timestamp(row.get("next_attempt_at")),
            last_attempt_at=_format_timestamp(row.get("last_attempt_at")),
            sent_at=_format_timestamp(row.get("sent_at")),
            provider_name=_as_str(row.get("provider_name")),
            provider_message_id=_as_str(row.get("provider_message_id")),
            last_error=_as_str(row.get("last_error")),
            lease_owner=_as_str(row.get("lease_owner")),
            lease_until=_format_timestamp(row.get("lease_until")),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
        )

    @staticmethod
    def _row_to_check_run(row: dict[str, object]) -> TrackingCheckRun:
        return TrackingCheckRun(
            run_id=str(row["run_id"]),
            item_id=str(row["item_id"]),
            scheduled_for=_format_timestamp(row.get("scheduled_for")),
            started_at=_format_timestamp(row.get("started_at")),
            finished_at=_format_timestamp(row.get("finished_at")),
            status=str(row["status"]),
            attempt=int(row["attempt"]),
            error=_as_str(row.get("error")),
            job_key=_as_str(row.get("job_key")),
        )

    @staticmethod
    def _row_to_bootstrap_idempotency(
        row: dict[str, object],
    ) -> TrackingBootstrapIdempotencyRecord:
        return TrackingBootstrapIdempotencyRecord(
            idempotency_key=str(row["idempotency_key"]),
            request_hash=str(row["request_hash"]),
            item_id=_as_str(row.get("item_id")),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
        )


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _format_timestamp(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _as_str(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)
