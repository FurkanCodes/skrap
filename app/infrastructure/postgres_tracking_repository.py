from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from time import sleep
import uuid

from psycopg.errors import DeadlockDetected
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.tracking.domain import (
    AppUser,
    AlertEvent,
    AffiliateClickEvent,
    BillingSubscription,
    BillingSubscriptionApplyResult,
    NotificationEvent,
    NotificationEventStatus,
    PriceSnapshot,
    TelegramConnectSession,
    TrackedItem,
    TrackingBootstrapIdempotencyRecord,
    TrackingCheckRun,
    TrackingCheckRunStatus,
    UsageCounter,
    UserEntitlement,
)

_SCHEMA_INITIALIZATION_LOCK_ID = 740_595_379_821_391_147

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracked_items (
        item_id TEXT PRIMARY KEY,
        user_id TEXT,
        source_url TEXT NOT NULL,
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
        created_via TEXT CHECK (created_via IN ('dashboard', 'extension')),
        status TEXT NOT NULL CHECK (status IN ('active', 'paused')),
        check_interval_minutes INTEGER NOT NULL CHECK (check_interval_minutes >= 1),
        target_amount TEXT,
        notify_on_price_increase BOOLEAN NOT NULL DEFAULT FALSE,
        notify_on_stock_change BOOLEAN NOT NULL DEFAULT FALSE,
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
    CREATE TABLE IF NOT EXISTS app_users (
        user_id TEXT PRIMARY KEY,
        auth_provider TEXT NOT NULL,
        auth_subject TEXT NOT NULL,
        email TEXT,
        display_name TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(auth_provider, auth_subject)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_plans (
        plan_key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        max_active_trackers INTEGER NOT NULL CHECK (max_active_trackers >= 0),
        min_check_interval_minutes INTEGER NOT NULL CHECK (min_check_interval_minutes >= 1),
        monthly_check_units INTEGER NOT NULL CHECK (monthly_check_units >= 0),
        allowed_channels TEXT NOT NULL,
        overage_policy TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_subscriptions (
        subscription_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        provider_customer_id TEXT,
        provider_subscription_id TEXT,
        provider_price_id TEXT,
        plan_key TEXT REFERENCES billing_plans(plan_key),
        status TEXT NOT NULL,
        current_period_start TIMESTAMPTZ,
        current_period_end TIMESTAMPTZ,
        next_billed_at TIMESTAMPTZ,
        cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
        last_event_id TEXT,
        last_event_occurred_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_entitlements (
        user_id TEXT PRIMARY KEY REFERENCES app_users(user_id) ON DELETE CASCADE,
        plan_key TEXT NOT NULL REFERENCES billing_plans(plan_key),
        max_active_trackers INTEGER NOT NULL CHECK (max_active_trackers >= 0),
        min_check_interval_minutes INTEGER NOT NULL CHECK (min_check_interval_minutes >= 1),
        monthly_check_units INTEGER NOT NULL CHECK (monthly_check_units >= 0),
        allowed_channels TEXT NOT NULL,
        overage_policy TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_usage_counters (
        counter_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        consumed_check_units INTEGER NOT NULL DEFAULT 0 CHECK (consumed_check_units >= 0),
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(user_id, period_start, period_end)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracker_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL REFERENCES tracked_items(item_id) ON DELETE CASCADE,
        checked_at TIMESTAMPTZ NOT NULL,
        scheduled_for TIMESTAMPTZ,
        success BOOLEAN NOT NULL,
        amount TEXT,
        currency TEXT,
        raw_text TEXT,
        source TEXT,
        error TEXT,
        availability TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracker_runs (
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
        snapshot_id TEXT NOT NULL REFERENCES tracker_snapshots(snapshot_id) ON DELETE CASCADE,
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
    CREATE TABLE IF NOT EXISTS outbound_notifications (
        notification_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        alert_event_id TEXT REFERENCES alert_events(event_id) ON DELETE CASCADE,
        item_id TEXT NOT NULL REFERENCES tracked_items(item_id) ON DELETE CASCADE,
        channel TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'sending', 'sent', 'failed', 'dead')),
        attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
        max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
        next_attempt_at TIMESTAMPTZ,
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
    CREATE TABLE IF NOT EXISTS telegram_connect_sessions (
        user_id TEXT,
        state TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('pending', 'connected', 'expired')),
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        connected_at TIMESTAMPTZ,
        telegram_user_id TEXT,
        telegram_chat_id TEXT,
        telegram_username TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_runtime_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS affiliate_click_events (
        event_id TEXT PRIMARY KEY,
        item_id TEXT,
        user_id TEXT,
        surface TEXT NOT NULL,
        merchant_domain TEXT,
        rule_id TEXT,
        is_affiliate BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tracked_items_due_active
      ON tracked_items(next_check_at, item_id)
      WHERE status = 'active'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_items_user_source_unique
      ON tracked_items(user_id, source_url)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tracked_items_user_updated
      ON tracked_items(user_id, updated_at DESC)
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
      ON tracker_snapshots(item_id, checked_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_price_snapshots_item_schedule_unique
      ON tracker_snapshots(item_id, scheduled_for)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_check_runs_item_started
      ON tracker_runs(item_id, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_check_runs_item_scheduled
      ON tracker_runs(item_id, scheduled_for, attempt DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_check_runs_job_key_unique
      ON tracker_runs(job_key)
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
      ON outbound_notifications(status, next_attempt_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_events_lease_until
      ON outbound_notifications(lease_until)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_events_item_created
      ON outbound_notifications(item_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_connect_sessions_status_expires
      ON telegram_connect_sessions(status, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_connect_sessions_user_status
      ON telegram_connect_sessions(user_id, status, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_affiliate_click_events_created
      ON affiliate_click_events(created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_affiliate_click_events_item_created
      ON affiliate_click_events(item_id, created_at DESC)
    """,
)

_LEGACY_TABLE_RENAMES: tuple[tuple[str, str], ...] = (
    ("users", "app_users"),
    ("plans", "billing_plans"),
    ("user_subscriptions", "billing_subscriptions"),
    ("entitlements", "billing_entitlements"),
    ("usage_counters", "billing_usage_counters"),
    ("price_snapshots", "tracker_snapshots"),
    ("tracking_check_runs", "tracker_runs"),
    ("notification_events", "outbound_notifications"),
)

_PLAN_DEFINITIONS: tuple[tuple[str, str, int, int, int, str, str], ...] = (
    ("free", "Free", 5, 720, 100, "telegram", "block"),
    ("plus", "Plus", 25, 120, 10_000, "telegram", "block"),
    ("pro", "Pro", 100, 30, 50_000, "telegram", "block"),
)

_DEFAULT_PLAN_KEY = "free"

_PLAN_DEFINITION_BY_KEY = {plan[0]: plan for plan in _PLAN_DEFINITIONS}

_SYNC_BILLING_ENTITLEMENT_FROM_PLAN_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION sync_billing_entitlement_limits_from_plan()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    selected_plan billing_plans%ROWTYPE;
BEGIN
    SELECT *
    INTO selected_plan
    FROM billing_plans
    WHERE plan_key = NEW.plan_key
    LIMIT 1;

    IF selected_plan.plan_key IS NULL THEN
        RAISE EXCEPTION 'unknown billing plan: %', NEW.plan_key;
    END IF;

    NEW.max_active_trackers := selected_plan.max_active_trackers;
    NEW.min_check_interval_minutes := selected_plan.min_check_interval_minutes;
    NEW.monthly_check_units := selected_plan.monthly_check_units;
    NEW.allowed_channels := selected_plan.allowed_channels;
    NEW.overage_policy := selected_plan.overage_policy;
    NEW.updated_at := COALESCE(NEW.updated_at, NOW());
    NEW.created_at := COALESCE(NEW.created_at, NEW.updated_at, NOW());

    RETURN NEW;
END;
$$
"""

_SYNC_BILLING_ENTITLEMENT_FROM_PLAN_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_sync_billing_entitlement_limits_from_plan
ON billing_entitlements;
CREATE TRIGGER trg_sync_billing_entitlement_limits_from_plan
BEFORE INSERT OR UPDATE OF plan_key
ON billing_entitlements
FOR EACH ROW
EXECUTE FUNCTION sync_billing_entitlement_limits_from_plan();
"""

_PROPAGATE_BILLING_PLAN_LIMITS_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION propagate_billing_plan_limits_to_entitlements()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE billing_entitlements
    SET
        max_active_trackers = NEW.max_active_trackers,
        min_check_interval_minutes = NEW.min_check_interval_minutes,
        monthly_check_units = NEW.monthly_check_units,
        allowed_channels = NEW.allowed_channels,
        overage_policy = NEW.overage_policy,
        updated_at = GREATEST(updated_at, COALESCE(NEW.updated_at, NOW()))
    WHERE plan_key = NEW.plan_key
      AND (
        max_active_trackers IS DISTINCT FROM NEW.max_active_trackers
        OR min_check_interval_minutes IS DISTINCT FROM NEW.min_check_interval_minutes
        OR monthly_check_units IS DISTINCT FROM NEW.monthly_check_units
        OR allowed_channels IS DISTINCT FROM NEW.allowed_channels
        OR overage_policy IS DISTINCT FROM NEW.overage_policy
      );
    RETURN NEW;
END;
$$
"""

_PROPAGATE_BILLING_PLAN_LIMITS_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_propagate_billing_plan_limits_to_entitlements
ON billing_plans;
CREATE TRIGGER trg_propagate_billing_plan_limits_to_entitlements
AFTER UPDATE OF
  max_active_trackers,
  min_check_interval_minutes,
  monthly_check_units,
  allowed_channels,
  overage_policy,
  updated_at
ON billing_plans
FOR EACH ROW
EXECUTE FUNCTION propagate_billing_plan_limits_to_entitlements();
"""

_SYNC_BILLING_ENTITLEMENTS_BACKFILL_SQL = """
UPDATE billing_entitlements
SET
    plan_key = plan_key,
    updated_at = COALESCE(updated_at, NOW())
"""


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
                conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_INITIALIZATION_LOCK_ID,))
                try:
                    with conn.transaction():
                        for old_name, new_name in _LEGACY_TABLE_RENAMES:
                            exists = conn.execute(
                                """
                                SELECT
                                  to_regclass(%s) IS NOT NULL AS old_exists,
                                  to_regclass(%s) IS NOT NULL AS new_exists
                                """,
                                (f"public.{old_name}", f"public.{new_name}"),
                            ).fetchone()
                            if not exists:
                                continue
                            if bool(exists["old_exists"]) and not bool(exists["new_exists"]):
                                conn.execute(
                                    f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"'
                                )

                    # Ensure legacy tables have user columns before user-scoped
                    # indexes are created in `_SCHEMA_STATEMENTS`.
                    conn.execute(_SCHEMA_STATEMENTS[0])
                    conn.execute(_SCHEMA_STATEMENTS[11])
                    conn.execute(
                        """
                        ALTER TABLE tracked_items
                        ADD COLUMN IF NOT EXISTS user_id TEXT
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE tracked_items
                        ADD COLUMN IF NOT EXISTS notify_on_price_increase BOOLEAN NOT NULL DEFAULT FALSE
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE tracked_items
                        ADD COLUMN IF NOT EXISTS notify_on_stock_change BOOLEAN NOT NULL DEFAULT FALSE
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE tracked_items
                        ADD COLUMN IF NOT EXISTS created_via TEXT
                        """
                    )
                    conn.execute(
                        """
                        DO $$
                        BEGIN
                            ALTER TABLE tracked_items
                            ADD CONSTRAINT tracked_items_created_via_check
                            CHECK (created_via IN ('dashboard', 'extension'));
                        EXCEPTION WHEN duplicate_object THEN
                            NULL;
                        END $$;
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE telegram_connect_sessions
                        ADD COLUMN IF NOT EXISTS user_id TEXT
                        """
                    )
                    for statement in _SCHEMA_STATEMENTS:
                        conn.execute(statement)
                    conn.execute(
                        """
                        ALTER TABLE billing_plans
                        ADD COLUMN IF NOT EXISTS max_active_trackers INTEGER
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_plans
                        ADD COLUMN IF NOT EXISTS min_check_interval_minutes INTEGER
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_plans
                        ADD COLUMN IF NOT EXISTS monthly_check_units INTEGER
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_plans
                        ADD COLUMN IF NOT EXISTS allowed_channels TEXT
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_plans
                        ADD COLUMN IF NOT EXISTS overage_policy TEXT
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_subscriptions
                        ADD COLUMN IF NOT EXISTS provider_price_id TEXT
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_subscriptions
                        ADD COLUMN IF NOT EXISTS plan_key TEXT
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_subscriptions
                        ADD COLUMN IF NOT EXISTS current_period_start TIMESTAMPTZ
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_subscriptions
                        ADD COLUMN IF NOT EXISTS next_billed_at TIMESTAMPTZ
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_subscriptions
                        ADD COLUMN IF NOT EXISTS last_event_id TEXT
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE billing_subscriptions
                        ADD COLUMN IF NOT EXISTS last_event_occurred_at TIMESTAMPTZ
                        """
                    )
                    conn.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_billing_subscriptions_provider_subscription
                          ON billing_subscriptions(provider, provider_subscription_id)
                        """
                    )
                    conn.execute(
                        """
                        UPDATE tracked_items
                        SET user_id = COALESCE(user_id, 'legacy_owner')
                        WHERE user_id IS NULL
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE tracker_snapshots
                        ADD COLUMN IF NOT EXISTS availability TEXT
                        """
                    )
                    conn.execute(
                        """
                        UPDATE telegram_connect_sessions
                        SET user_id = COALESCE(user_id, 'legacy_owner')
                        WHERE user_id IS NULL
                        """
                    )
                    conn.execute(
                        """
                        ALTER TABLE tracked_items
                        DROP CONSTRAINT IF EXISTS tracked_items_source_url_key
                        """
                    )
                    # Older deployments created outbound_notifications.alert_event_id
                    # as NOT NULL. Monitor-mode notifications don't have alert rows.
                    conn.execute(
                        """
                        ALTER TABLE outbound_notifications
                        ALTER COLUMN alert_event_id DROP NOT NULL
                        """
                    )
                    # Sent/dead notifications should clear their retry schedule.
                    conn.execute(
                        """
                        ALTER TABLE outbound_notifications
                        ALTER COLUMN next_attempt_at DROP NOT NULL
                        """
                    )
                    now = datetime.now(tz=UTC)
                    for (
                        plan_key,
                        name,
                        max_active_trackers,
                        min_check_interval_minutes,
                        monthly_check_units,
                        allowed_channels,
                        overage_policy,
                    ) in _PLAN_DEFINITIONS:
                        conn.execute(
                            """
                            INSERT INTO billing_plans (
                                plan_key,
                                name,
                                max_active_trackers,
                                min_check_interval_minutes,
                                monthly_check_units,
                                allowed_channels,
                                overage_policy,
                                created_at,
                                updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT(plan_key) DO UPDATE SET
                                name = EXCLUDED.name,
                                max_active_trackers = EXCLUDED.max_active_trackers,
                                min_check_interval_minutes = EXCLUDED.min_check_interval_minutes,
                                monthly_check_units = EXCLUDED.monthly_check_units,
                                allowed_channels = EXCLUDED.allowed_channels,
                                overage_policy = EXCLUDED.overage_policy,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (
                                plan_key,
                                name,
                                max_active_trackers,
                                min_check_interval_minutes,
                                monthly_check_units,
                                allowed_channels,
                                overage_policy,
                                now,
                                now,
                            ),
                        )

                        conn.execute(_SYNC_BILLING_ENTITLEMENT_FROM_PLAN_FUNCTION_SQL)
                        conn.execute(_SYNC_BILLING_ENTITLEMENT_FROM_PLAN_TRIGGER_SQL)
                        conn.execute(_PROPAGATE_BILLING_PLAN_LIMITS_FUNCTION_SQL)
                        conn.execute(_PROPAGATE_BILLING_PLAN_LIMITS_TRIGGER_SQL)
                        conn.execute(
                            _SYNC_BILLING_ENTITLEMENTS_BACKFILL_SQL,
                        )
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_INITIALIZATION_LOCK_ID,))

    def close(self) -> None:
        """Close pooled connections (useful in tests/shutdown hooks)."""
        with self._lock:
            self._pool.close()

    def upsert_app_user(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
        email: str | None,
        display_name: str | None,
        now: str,
    ) -> AppUser:
        now_dt = _parse_timestamp(now)
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO app_users (
                    user_id,
                    auth_provider,
                    auth_subject,
                    email,
                    display_name,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(auth_provider, auth_subject) DO UPDATE SET
                    email = EXCLUDED.email,
                    display_name = EXCLUDED.display_name,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    f"usr_{uuid.uuid4().hex[:20]}",
                    auth_provider,
                    auth_subject,
                    email,
                    display_name,
                    now_dt,
                    now_dt,
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to upsert app user")
        return self._row_to_app_user(row)

    def get_app_user_by_auth_subject(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
    ) -> AppUser | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM app_users
                WHERE auth_provider = %s
                  AND auth_subject = %s
                LIMIT 1
                """,
                (auth_provider, auth_subject),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_app_user(row)

    def get_app_user(self, *, user_id: str) -> AppUser | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM app_users
                WHERE user_id = %s
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_app_user(row)

    def _default_plan_values(self) -> tuple[str, int, int, int, str, str]:
        plan = _PLAN_DEFINITION_BY_KEY.get(_DEFAULT_PLAN_KEY)
        if plan is None:  # pragma: no cover - defensive guard
            raise RuntimeError("missing default plan configuration")
        (
            plan_key,
            _name,
            max_active_trackers,
            min_check_interval_minutes,
            monthly_check_units,
            allowed_channels,
            overage_policy,
        ) = plan
        return (
            plan_key,
            max_active_trackers,
            min_check_interval_minutes,
            monthly_check_units,
            allowed_channels,
            overage_policy,
        )

    def _resolve_plan_values(
        self,
        *,
        conn,
        plan_key: str,
        fallback: tuple[str, int, int, int, str, str],
    ) -> tuple[str, int, int, int, str, str]:
        row = conn.execute(
            """
            SELECT
                plan_key,
                max_active_trackers,
                min_check_interval_minutes,
                monthly_check_units,
                allowed_channels,
                overage_policy
            FROM billing_plans
            WHERE plan_key = %s
            LIMIT 1
            """,
            (plan_key,),
        ).fetchone()
        if row is None:
            return fallback
        return (
            str(row["plan_key"]),
            int(row["max_active_trackers"]),
            int(row["min_check_interval_minutes"]),
            int(row["monthly_check_units"]),
            str(row["allowed_channels"]),
            str(row["overage_policy"]),
        )

    def ensure_default_user_entitlement(self, *, user_id: str, now: str) -> None:
        now_dt = _parse_timestamp(now)
        with self._pool.connection() as conn:
            default_values = self._default_plan_values()
            (
                default_plan_key,
                default_max_active_trackers,
                default_min_check_interval_minutes,
                default_monthly_check_units,
                default_allowed_channels,
                default_overage_policy,
            ) = self._resolve_plan_values(
                conn=conn,
                plan_key=default_values[0],
                fallback=default_values,
            )
            conn.execute(
                """
                INSERT INTO billing_entitlements (
                    user_id,
                    plan_key,
                    max_active_trackers,
                    min_check_interval_minutes,
                    monthly_check_units,
                    allowed_channels,
                    overage_policy,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (
                    user_id,
                    default_plan_key,
                    default_max_active_trackers,
                    default_min_check_interval_minutes,
                    default_monthly_check_units,
                    default_allowed_channels,
                    default_overage_policy,
                    now_dt,
                    now_dt,
                ),
            )
            conn.execute(
                """
                INSERT INTO billing_subscriptions (
                    subscription_id,
                    user_id,
                    provider,
                    provider_customer_id,
                    provider_subscription_id,
                    status,
                    current_period_end,
                    cancel_at_period_end,
                    created_at,
                    updated_at
                ) VALUES (%s, %s, 'internal', NULL, NULL, 'active', NULL, FALSE, %s, %s)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (f"sub_{uuid.uuid4().hex[:20]}", user_id, now_dt, now_dt),
            )

    def get_user_entitlement(self, *, user_id: str) -> UserEntitlement | None:
        for attempt in range(3):
            try:
                with self._pool.connection() as conn:
                    row = conn.execute(
                        """
                        SELECT
                            entitlement.user_id,
                            entitlement.plan_key,
                            COALESCE(plan.max_active_trackers, entitlement.max_active_trackers)
                                AS max_active_trackers,
                            COALESCE(
                                plan.min_check_interval_minutes,
                                entitlement.min_check_interval_minutes
                            ) AS min_check_interval_minutes,
                            COALESCE(plan.monthly_check_units, entitlement.monthly_check_units)
                                AS monthly_check_units,
                            COALESCE(plan.allowed_channels, entitlement.allowed_channels)
                                AS allowed_channels,
                            COALESCE(plan.overage_policy, entitlement.overage_policy)
                                AS overage_policy,
                            entitlement.created_at,
                            GREATEST(
                                entitlement.updated_at,
                                COALESCE(plan.updated_at, entitlement.updated_at)
                            ) AS updated_at
                        FROM billing_entitlements AS entitlement
                        LEFT JOIN billing_plans AS plan
                          ON plan.plan_key = entitlement.plan_key
                        WHERE entitlement.user_id = %s
                        LIMIT 1
                        """,
                        (user_id,),
                    ).fetchone()
                break
            except DeadlockDetected:
                if attempt == 2:
                    raise
                sleep(0.05 * (attempt + 1))
        if row is None:
            return None
        return self._row_to_user_entitlement(row)

    def get_current_usage_counter(self, *, user_id: str, now: str) -> UsageCounter:
        now_dt = _parse_timestamp(now)
        if now_dt is None:  # pragma: no cover - defensive guard
            raise RuntimeError("now must be provided")
        period_start, period_end = _usage_period_bounds(now_dt)

        with self._pool.connection() as conn:
            with conn.transaction():
                self._ensure_usage_counter(
                    conn=conn,
                    user_id=user_id,
                    period_start=period_start,
                    period_end=period_end,
                    now=now_dt,
                )
                row = conn.execute(
                    """
                    SELECT *
                    FROM billing_usage_counters
                    WHERE user_id = %s
                      AND period_start = %s
                      AND period_end = %s
                    LIMIT 1
                    """,
                    (user_id, period_start, period_end),
                ).fetchone()
        if row is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to resolve current usage counter")
        return self._row_to_usage_counter(row)

    def get_billing_subscription_for_user(self, *, user_id: str) -> BillingSubscription | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM billing_subscriptions
                WHERE user_id = %s
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_billing_subscription(row)

    def get_billing_subscription_by_provider_subscription_id(
        self,
        *,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscription | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM billing_subscriptions
                WHERE provider = %s
                  AND provider_subscription_id = %s
                LIMIT 1
                """,
                (provider, provider_subscription_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_billing_subscription(row)

    def apply_billing_subscription_update(
        self,
        *,
        user_id: str,
        provider: str,
        provider_customer_id: str | None,
        provider_subscription_id: str,
        provider_price_id: str | None,
        plan_key: str | None,
        status: str,
        current_period_start: str | None,
        current_period_end: str | None,
        next_billed_at: str | None,
        cancel_at_period_end: bool,
        last_event_id: str,
        last_event_occurred_at: str,
        now: str,
    ) -> BillingSubscriptionApplyResult:
        now_dt = _parse_timestamp(now)
        event_dt = _parse_timestamp(last_event_occurred_at)
        if now_dt is None or event_dt is None:  # pragma: no cover - defensive guard
            raise RuntimeError("billing timestamps must be provided")

        active_plan_key = (
            plan_key if status in {"active", "trialing", "past_due"} else _DEFAULT_PLAN_KEY
        )
        if active_plan_key not in _PLAN_DEFINITION_BY_KEY:
            active_plan_key = _DEFAULT_PLAN_KEY

        with self._pool.connection() as conn:
            with conn.transaction():
                current = conn.execute(
                    """
                    SELECT *
                    FROM billing_subscriptions
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                ).fetchone()
                if current is not None:
                    current_event_id = _as_str(current.get("last_event_id"))
                    current_event_at = _parse_timestamp(
                        _format_timestamp(current.get("last_event_occurred_at"))
                    )
                    if current_event_id == last_event_id:
                        return BillingSubscriptionApplyResult(
                            processed=False,
                            reason="duplicate_event",
                            subscription=self._row_to_billing_subscription(current),
                        )
                    if current_event_at is not None and current_event_at > event_dt:
                        return BillingSubscriptionApplyResult(
                            processed=False,
                            reason="stale_event",
                            subscription=self._row_to_billing_subscription(current),
                        )

                row = conn.execute(
                    """
                    INSERT INTO billing_subscriptions (
                        subscription_id,
                        user_id,
                        provider,
                        provider_customer_id,
                        provider_subscription_id,
                        provider_price_id,
                        plan_key,
                        status,
                        current_period_start,
                        current_period_end,
                        next_billed_at,
                        cancel_at_period_end,
                        last_event_id,
                        last_event_occurred_at,
                        created_at,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        provider = EXCLUDED.provider,
                        provider_customer_id = EXCLUDED.provider_customer_id,
                        provider_subscription_id = EXCLUDED.provider_subscription_id,
                        provider_price_id = EXCLUDED.provider_price_id,
                        plan_key = EXCLUDED.plan_key,
                        status = EXCLUDED.status,
                        current_period_start = EXCLUDED.current_period_start,
                        current_period_end = EXCLUDED.current_period_end,
                        next_billed_at = EXCLUDED.next_billed_at,
                        cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                        last_event_id = EXCLUDED.last_event_id,
                        last_event_occurred_at = EXCLUDED.last_event_occurred_at,
                        updated_at = EXCLUDED.updated_at
                    RETURNING *
                    """,
                    (
                        f"sub_{uuid.uuid4().hex[:20]}",
                        user_id,
                        provider,
                        provider_customer_id,
                        provider_subscription_id,
                        provider_price_id,
                        plan_key,
                        status,
                        _parse_timestamp(current_period_start),
                        _parse_timestamp(current_period_end),
                        _parse_timestamp(next_billed_at),
                        cancel_at_period_end,
                        last_event_id,
                        event_dt,
                        now_dt,
                        now_dt,
                    ),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO billing_entitlements (
                        user_id,
                        plan_key,
                        max_active_trackers,
                        min_check_interval_minutes,
                        monthly_check_units,
                        allowed_channels,
                        overage_policy,
                        created_at,
                        updated_at
                    ) VALUES (%s, %s, 0, 1, 0, '', 'block', %s, %s)
                    ON CONFLICT(user_id) DO UPDATE SET
                        plan_key = EXCLUDED.plan_key,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (user_id, active_plan_key, now_dt, now_dt),
                )

        if row is None:  # pragma: no cover - defensive guard
            return BillingSubscriptionApplyResult(
                processed=False,
                reason="missing_subscription",
                subscription=None,
            )
        return BillingSubscriptionApplyResult(
            processed=True,
            reason=None,
            subscription=self._row_to_billing_subscription(row),
        )

    def try_consume_check_units(
        self,
        *,
        user_id: str,
        units: int,
        monthly_limit: int,
        now: str,
    ) -> bool:
        if units < 1:
            return True
        if monthly_limit < 0:
            monthly_limit = 0

        now_dt = _parse_timestamp(now)
        if now_dt is None:  # pragma: no cover - defensive guard
            return False
        period_start, period_end = _usage_period_bounds(now_dt)

        with self._pool.connection() as conn:
            with conn.transaction():
                self._ensure_usage_counter(
                    conn=conn,
                    user_id=user_id,
                    period_start=period_start,
                    period_end=period_end,
                    now=now_dt,
                )
                row = conn.execute(
                    """
                    UPDATE billing_usage_counters
                    SET consumed_check_units = consumed_check_units + %s,
                        updated_at = %s
                    WHERE user_id = %s
                      AND period_start = %s
                      AND period_end = %s
                      AND (consumed_check_units + %s) <= %s
                    RETURNING counter_id
                    """,
                    (
                        units,
                        now_dt,
                        user_id,
                        period_start,
                        period_end,
                        units,
                        monthly_limit,
                    ),
                ).fetchone()
        return row is not None

    def assign_all_legacy_data_to_user(self, *, user_id: str, now: str) -> None:
        now_dt = _parse_timestamp(now)
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE tracked_items
                    SET user_id = %s,
                        updated_at = %s
                    WHERE user_id IS NULL OR user_id = 'legacy_owner'
                    """,
                    (user_id, now_dt),
                )
                conn.execute(
                    """
                    UPDATE telegram_connect_sessions
                    SET user_id = %s
                    WHERE user_id IS NULL OR user_id = 'legacy_owner'
                    """,
                    (user_id,),
                )

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

    def list_items_for_user(self, user_id: str) -> list[TrackedItem]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE user_id = %s
                ORDER BY updated_at DESC
                """,
                (user_id,),
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

    def get_item_for_user(self, *, item_id: str, user_id: str) -> TrackedItem | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE item_id = %s
                  AND user_id = %s
                """,
                (item_id, user_id),
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

    def get_item_by_source_url_for_user(
        self,
        *,
        source_url: str,
        user_id: str,
    ) -> TrackedItem | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tracked_items
                WHERE source_url = %s
                  AND user_id = %s
                LIMIT 1
                """,
                (source_url, user_id),
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
                    user_id,
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
                    created_via,
                    status,
                    check_interval_minutes,
                    target_amount,
                    notify_on_price_increase,
                    notify_on_stock_change,
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
                    %(user_id)s,
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
                    %(created_via)s,
                    %(status)s,
                    %(check_interval_minutes)s,
                    %(target_amount)s,
                    %(notify_on_price_increase)s,
                    %(notify_on_stock_change)s,
                    %(created_at)s,
                    %(updated_at)s,
                    %(last_checked_at)s,
                    %(next_check_at)s,
                    %(lease_owner)s,
                    %(lease_until)s,
                    %(last_check_started_at)s,
                    %(last_check_finished_at)s
                )
                ON CONFLICT(user_id, source_url) DO UPDATE SET
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
                    notify_on_price_increase = EXCLUDED.notify_on_price_increase,
                    notify_on_stock_change = EXCLUDED.notify_on_stock_change,
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
                    user_id,
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
                        created_via,
                        status,
                        check_interval_minutes,
                        target_amount,
                        notify_on_price_increase,
                        notify_on_stock_change,
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
                        %(user_id)s,
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
                        %(created_via)s,
                        %(status)s,
                        %(check_interval_minutes)s,
                        %(target_amount)s,
                        %(notify_on_price_increase)s,
                        %(notify_on_stock_change)s,
                        %(created_at)s,
                        %(updated_at)s,
                        %(last_checked_at)s,
                        %(next_check_at)s,
                        %(lease_owner)s,
                        %(lease_until)s,
                        %(last_check_started_at)s,
                        %(last_check_finished_at)s
                    )
                    ON CONFLICT(user_id, source_url) DO UPDATE SET
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
                        notify_on_price_increase = EXCLUDED.notify_on_price_increase,
                        notify_on_stock_change = EXCLUDED.notify_on_stock_change,
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
                FROM tracker_snapshots
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
                INSERT INTO outbound_notifications (
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
                    FROM outbound_notifications
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
                    UPDATE outbound_notifications
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
                    FROM outbound_notifications
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
                UPDATE outbound_notifications
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
                FROM outbound_notifications
                WHERE item_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_notification_event(row) for row in rows]

    def save_affiliate_click_event(self, event: AffiliateClickEvent) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO affiliate_click_events (
                    event_id,
                    item_id,
                    user_id,
                    surface,
                    merchant_domain,
                    rule_id,
                    is_affiliate,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.item_id,
                    event.user_id,
                    event.surface,
                    event.merchant_domain,
                    event.rule_id,
                    event.is_affiliate,
                    _parse_timestamp(event.created_at),
                ),
            )

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
                    FROM tracker_runs
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
                    FROM tracker_runs
                    WHERE item_id = %s
                      AND scheduled_for = %s
                    """,
                    (item_id, scheduled_for_dt),
                ).fetchone()
                attempt = int((attempt_row["max_attempt"] if attempt_row is not None else 0) or 0) + 1

                inserted = conn.execute(
                    """
                    INSERT INTO tracker_runs (
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
                        FROM tracker_runs
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
                UPDATE tracker_runs
                SET status = %s,
                    finished_at = %s,
                    error = %s
                WHERE run_id = %s
                """,
                (status, _parse_timestamp(finished_at), error, run_id),
            )

    def abandon_check_run(
        self,
        *,
        run_id: str,
        finished_at: str,
        error: str,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE tracker_runs
                SET status = 'failed',
                    finished_at = %s,
                    error = %s,
                    job_key = NULL
                WHERE run_id = %s
                  AND status = 'running'
                  AND finished_at IS NULL
                """,
                (_parse_timestamp(finished_at), error, run_id),
            )

    def list_check_runs(self, item_id: str, limit: int) -> list[TrackingCheckRun]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM tracker_runs
                WHERE item_id = %s
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_check_run(row) for row in rows]

    def get_bootstrap_idempotency_record(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        scoped_key = _scope_idempotency_key(user_id=user_id, key=idempotency_key)
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM tracking_bootstrap_idempotency
                WHERE idempotency_key = %s
                """,
                (scoped_key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_bootstrap_idempotency(row)

    def save_bootstrap_idempotency_record(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        now_dt = _parse_timestamp(now)
        scoped_key = _scope_idempotency_key(user_id=user_id, key=idempotency_key)
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
                (scoped_key, request_hash, item_id, now_dt, now_dt),
            )

    def create_telegram_connect_session(self, session: TelegramConnectSession) -> TelegramConnectSession:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO telegram_connect_sessions (
                    user_id,
                    state,
                    status,
                    created_at,
                    expires_at,
                    connected_at,
                    telegram_user_id,
                    telegram_chat_id,
                    telegram_username
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(state) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at,
                    connected_at = EXCLUDED.connected_at,
                    telegram_user_id = EXCLUDED.telegram_user_id,
                    telegram_chat_id = EXCLUDED.telegram_chat_id,
                    telegram_username = EXCLUDED.telegram_username
                RETURNING *
                """,
                (
                    session.user_id,
                    session.state,
                    session.status,
                    _parse_timestamp(session.created_at),
                    _parse_timestamp(session.expires_at),
                    _parse_timestamp(session.connected_at),
                    session.telegram_user_id,
                    session.telegram_chat_id,
                    session.telegram_username,
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Failed to persist telegram connect session")
        return self._row_to_telegram_connect_session(row)

    def get_telegram_connect_session(
        self,
        *,
        user_id: str,
        state: str,
    ) -> TelegramConnectSession | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM telegram_connect_sessions
                WHERE state = %s
                  AND user_id = %s
                """,
                (state, user_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_telegram_connect_session(row)

    def get_telegram_connect_session_by_state(self, state: str) -> TelegramConnectSession | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM telegram_connect_sessions
                WHERE state = %s
                LIMIT 1
                """,
                (state,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_telegram_connect_session(row)

    def mark_telegram_connect_session_connected(
        self,
        *,
        user_id: str,
        state: str,
        connected_at: str,
        telegram_user_id: str,
        telegram_chat_id: str,
        telegram_username: str | None,
    ) -> TelegramConnectSession | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE telegram_connect_sessions
                SET status = 'connected',
                    connected_at = %s,
                    telegram_user_id = %s,
                    telegram_chat_id = %s,
                    telegram_username = %s
                WHERE state = %s
                  AND user_id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    _parse_timestamp(connected_at),
                    telegram_user_id,
                    telegram_chat_id,
                    telegram_username,
                    state,
                    user_id,
                ),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_telegram_connect_session(row)

    def mark_telegram_connect_session_expired(
        self,
        *,
        user_id: str,
        state: str,
    ) -> TelegramConnectSession | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE telegram_connect_sessions
                SET status = 'expired'
                WHERE state = %s
                  AND user_id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (state, user_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_telegram_connect_session(row)

    def set_active_telegram_chat(
        self,
        *,
        user_id: str,
        chat_id: str,
        updated_at: str,
    ) -> None:
        updated_at_dt = _parse_timestamp(updated_at)
        key = _active_telegram_chat_key(user_id)
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO app_runtime_settings (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT(key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at
                """,
                (key, chat_id, updated_at_dt),
            )

    def get_active_telegram_chat(self, *, user_id: str) -> str | None:
        key = _active_telegram_chat_key(user_id)
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT value
                FROM app_runtime_settings
                WHERE key = %s
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return _as_str(row.get("value"))

    @staticmethod
    def _ensure_usage_counter(
        *,
        conn,
        user_id: str,
        period_start: datetime,
        period_end: datetime,
        now: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO billing_usage_counters (
                counter_id,
                user_id,
                period_start,
                period_end,
                consumed_check_units,
                created_at,
                updated_at
            ) VALUES (%s, %s, %s, %s, 0, %s, %s)
            ON CONFLICT(user_id, period_start, period_end) DO NOTHING
            """,
            (
                f"usc_{uuid.uuid4().hex[:20]}",
                user_id,
                period_start,
                period_end,
                now,
                now,
            ),
        )

    def _insert_snapshot(self, conn, snapshot: PriceSnapshot) -> None:
        conn.execute(
            """
            INSERT INTO tracker_snapshots (
                snapshot_id,
                item_id,
                checked_at,
                scheduled_for,
                success,
                amount,
                currency,
                raw_text,
                source,
                error,
                availability
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(item_id, scheduled_for) DO UPDATE SET
                checked_at = EXCLUDED.checked_at,
                success = CASE
                    WHEN tracker_snapshots.success THEN TRUE
                    ELSE EXCLUDED.success
                END,
                amount = CASE
                    WHEN EXCLUDED.success OR NOT tracker_snapshots.success
                    THEN EXCLUDED.amount
                    ELSE tracker_snapshots.amount
                END,
                currency = CASE
                    WHEN EXCLUDED.success OR NOT tracker_snapshots.success
                    THEN EXCLUDED.currency
                    ELSE tracker_snapshots.currency
                END,
                raw_text = CASE
                    WHEN EXCLUDED.success OR NOT tracker_snapshots.success
                    THEN EXCLUDED.raw_text
                    ELSE tracker_snapshots.raw_text
                END,
                source = CASE
                    WHEN EXCLUDED.success OR NOT tracker_snapshots.success
                    THEN EXCLUDED.source
                    ELSE tracker_snapshots.source
                END,
                error = CASE
                    WHEN EXCLUDED.success THEN NULL
                    WHEN tracker_snapshots.success THEN tracker_snapshots.error
                    ELSE EXCLUDED.error
                END,
                availability = CASE
                    WHEN EXCLUDED.success OR NOT tracker_snapshots.success
                    THEN EXCLUDED.availability
                    ELSE tracker_snapshots.availability
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
                snapshot.availability,
            ),
        )

    @staticmethod
    def _item_params(item: TrackedItem) -> dict[str, object | None]:
        return {
            "item_id": item.item_id,
            "user_id": item.user_id,
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
            "created_via": item.created_via,
            "status": item.status,
            "check_interval_minutes": item.check_interval_minutes,
            "target_amount": item.target_amount,
            "notify_on_price_increase": bool(item.notify_on_price_increase),
            "notify_on_stock_change": bool(item.notify_on_stock_change),
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
            user_id=str(row["user_id"]),
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
            created_via=_as_str(row.get("created_via")),
            status=str(row["status"]),
            check_interval_minutes=int(row["check_interval_minutes"]),
            target_amount=_as_str(row.get("target_amount")),
            notify_on_price_increase=bool(row.get("notify_on_price_increase", False)),
            notify_on_stock_change=bool(row.get("notify_on_stock_change", False)),
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
            availability=_as_str(row.get("availability")),
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
            alert_event_id=_as_str(row.get("alert_event_id")),
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
    def _row_to_app_user(row: dict[str, object]) -> AppUser:
        return AppUser(
            user_id=str(row["user_id"]),
            auth_provider=str(row["auth_provider"]),
            auth_subject=str(row["auth_subject"]),
            email=_as_str(row.get("email")),
            display_name=_as_str(row.get("display_name")),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
        )

    @staticmethod
    def _row_to_user_entitlement(row: dict[str, object]) -> UserEntitlement:
        return UserEntitlement(
            user_id=str(row["user_id"]),
            plan_key=str(row["plan_key"]),
            max_active_trackers=int(row["max_active_trackers"]),
            min_check_interval_minutes=int(row["min_check_interval_minutes"]),
            monthly_check_units=int(row["monthly_check_units"]),
            allowed_channels=str(row["allowed_channels"]),
            overage_policy=str(row["overage_policy"]),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
        )

    @staticmethod
    def _row_to_usage_counter(row: dict[str, object]) -> UsageCounter:
        return UsageCounter(
            counter_id=str(row["counter_id"]),
            user_id=str(row["user_id"]),
            period_start=_format_timestamp(row.get("period_start")),
            period_end=_format_timestamp(row.get("period_end")),
            consumed_check_units=int(row["consumed_check_units"]),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
        )

    @staticmethod
    def _row_to_billing_subscription(row: dict[str, object]) -> BillingSubscription:
        return BillingSubscription(
            subscription_id=str(row["subscription_id"]),
            user_id=str(row["user_id"]),
            provider=str(row["provider"]),
            provider_customer_id=_as_str(row.get("provider_customer_id")),
            provider_subscription_id=_as_str(row.get("provider_subscription_id")),
            provider_price_id=_as_str(row.get("provider_price_id")),
            plan_key=_as_str(row.get("plan_key")),
            status=str(row["status"]),
            current_period_start=_format_timestamp(row.get("current_period_start")),
            current_period_end=_format_timestamp(row.get("current_period_end")),
            next_billed_at=_format_timestamp(row.get("next_billed_at")),
            cancel_at_period_end=bool(row.get("cancel_at_period_end", False)),
            last_event_id=_as_str(row.get("last_event_id")),
            last_event_occurred_at=_format_timestamp(row.get("last_event_occurred_at")),
            created_at=_format_timestamp(row.get("created_at")),
            updated_at=_format_timestamp(row.get("updated_at")),
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

    @staticmethod
    def _row_to_telegram_connect_session(
        row: dict[str, object],
    ) -> TelegramConnectSession:
        return TelegramConnectSession(
            user_id=str(row["user_id"]),
            state=str(row["state"]),
            status=str(row["status"]),
            created_at=_format_timestamp(row.get("created_at")),
            expires_at=_format_timestamp(row.get("expires_at")),
            connected_at=_format_timestamp(row.get("connected_at")),
            telegram_user_id=_as_str(row.get("telegram_user_id")),
            telegram_chat_id=_as_str(row.get("telegram_chat_id")),
            telegram_username=_as_str(row.get("telegram_username")),
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


def _usage_period_bounds(now: datetime) -> tuple[datetime, datetime]:
    period_start = datetime(year=now.year, month=now.month, day=1, tzinfo=UTC)
    if now.month == 12:
        period_end = datetime(year=now.year + 1, month=1, day=1, tzinfo=UTC)
    else:
        period_end = datetime(year=now.year, month=now.month + 1, day=1, tzinfo=UTC)
    return period_start, period_end


def _scope_idempotency_key(*, user_id: str, key: str) -> str:
    return f"{user_id}:{key}"


def _active_telegram_chat_key(user_id: str) -> str:
    return f"active_telegram_chat_id:{user_id}"
