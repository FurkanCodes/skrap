from __future__ import annotations

from typing import Protocol

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
    TrackingBootstrapIdempotencyRecord,
    TrackedItem,
    TrackingCheckRun,
    TrackingCheckRunStatus,
    UsageCounter,
    UserEntitlement,
)


class TrackingRepository(Protocol):
    """Persistence boundary for tracked items and snapshots."""

    def initialize(self) -> None:
        ...

    def list_items(self) -> list[TrackedItem]:
        ...

    def list_items_for_user(self, user_id: str) -> list[TrackedItem]:
        ...

    def get_item(self, item_id: str) -> TrackedItem | None:
        ...

    def get_item_for_user(self, *, item_id: str, user_id: str) -> TrackedItem | None:
        ...

    def get_item_by_source_url(self, source_url: str) -> TrackedItem | None:
        ...

    def get_item_by_source_url_for_user(
        self,
        *,
        source_url: str,
        user_id: str,
    ) -> TrackedItem | None:
        ...

    def save_item(self, item: TrackedItem) -> TrackedItem:
        ...

    def save_item_and_snapshot(self, item: TrackedItem, snapshot: PriceSnapshot) -> TrackedItem:
        ...

    def delete_item(self, item_id: str) -> bool:
        ...

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        ...

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        ...

    def create_alert_event(self, event: AlertEvent) -> bool:
        """Persist alert event idempotently.

        Returns True when a new event row is created, False when the event
        already exists (idempotency conflict).
        """
        ...

    def get_last_alert_event(
        self,
        *,
        item_id: str,
        rule_type: str,
    ) -> AlertEvent | None:
        ...

    def list_alert_events(self, item_id: str, limit: int) -> list[AlertEvent]:
        ...

    def enqueue_notification_event(self, event: NotificationEvent) -> bool:
        """Persist notification outbox row idempotently."""
        ...

    def claim_due_notification_events(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[NotificationEvent]:
        ...

    def claim_notification_events_by_ids(
        self,
        *,
        notification_ids: list[str],
        now: str,
        lease_owner: str,
        lease_until: str,
    ) -> list[NotificationEvent]:
        """Lease specific outbox rows by id when woken by the notification queue."""
        ...

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
        ...

    def list_notification_events(self, item_id: str, limit: int) -> list[NotificationEvent]:
        ...

    def save_affiliate_click_event(self, event: AffiliateClickEvent) -> None:
        ...

    def claim_due_items(
        self,
        *,
        now: str,
        lease_owner: str,
        lease_until: str,
        limit: int,
    ) -> list[TrackedItem]:
        ...

    def claim_due_items_by_ids(
        self,
        *,
        item_ids: list[str],
        now: str,
        lease_owner: str,
        lease_until: str,
    ) -> list[TrackedItem]:
        """Lease specific due active items woken by the due index."""
        ...

    def start_check_run(
        self,
        *,
        run_id: str,
        item_id: str,
        scheduled_for: str,
        job_key: str,
        started_at: str,
    ) -> TrackingCheckRun:
        ...

    def finish_check_run(
        self,
        *,
        run_id: str,
        status: TrackingCheckRunStatus,
        finished_at: str,
        error: str | None,
    ) -> None:
        ...

    def abandon_check_run(
        self,
        *,
        run_id: str,
        finished_at: str,
        error: str,
    ) -> None:
        ...

    def list_check_runs(self, item_id: str, limit: int) -> list[TrackingCheckRun]:
        ...

    def get_bootstrap_idempotency_record(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        ...

    def save_bootstrap_idempotency_record(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        ...

    def create_telegram_connect_session(self, session: TelegramConnectSession) -> TelegramConnectSession:
        ...

    def get_telegram_connect_session(
        self,
        *,
        user_id: str,
        state: str,
    ) -> TelegramConnectSession | None:
        ...

    def get_telegram_connect_session_by_state(self, state: str) -> TelegramConnectSession | None:
        ...

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
        ...

    def mark_telegram_connect_session_expired(
        self,
        *,
        user_id: str,
        state: str,
    ) -> TelegramConnectSession | None:
        ...

    def set_active_telegram_chat(
        self,
        *,
        user_id: str,
        chat_id: str,
        updated_at: str,
    ) -> None:
        ...

    def get_active_telegram_chat(self, *, user_id: str) -> str | None:
        ...

    def upsert_app_user(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
        email: str | None,
        display_name: str | None,
        now: str,
    ) -> AppUser:
        ...

    def get_app_user_by_auth_subject(
        self,
        *,
        auth_provider: str,
        auth_subject: str,
    ) -> AppUser | None:
        ...

    def get_app_user(self, *, user_id: str) -> AppUser | None:
        ...

    def ensure_default_user_entitlement(self, *, user_id: str, now: str) -> None:
        ...

    def get_user_entitlement(self, *, user_id: str) -> UserEntitlement | None:
        ...

    def get_current_usage_counter(self, *, user_id: str, now: str) -> UsageCounter:
        ...

    def get_billing_subscription_for_user(self, *, user_id: str) -> BillingSubscription | None:
        ...

    def get_billing_subscription_by_provider_subscription_id(
        self,
        *,
        provider: str,
        provider_subscription_id: str,
    ) -> BillingSubscription | None:
        ...

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
        ...

    def try_consume_check_units(
        self,
        *,
        user_id: str,
        units: int,
        monthly_limit: int,
        now: str,
    ) -> bool:
        ...

    def assign_all_legacy_data_to_user(self, *, user_id: str, now: str) -> None:
        ...
