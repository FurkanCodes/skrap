from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TrackingStatus = Literal["active", "paused"]
TrackingMode = Literal["monitor", "alert"]
TrackingCreatedVia = Literal["dashboard", "extension"]
TrackingCheckRunStatus = Literal["running", "succeeded", "failed"]
AlertRuleType = Literal["price_below"]
NotificationEventStatus = Literal["queued", "sending", "sent", "failed", "dead"]
TelegramConnectSessionStatus = Literal["pending", "connected", "expired"]
AuthProvider = Literal["better_auth"]


def derive_tracking_mode(target_amount: str | None) -> TrackingMode:
    """Infer tracking mode from target amount presence."""
    if target_amount is None:
        return "monitor"
    return "alert" if target_amount.strip() else "monitor"


@dataclass(frozen=True, kw_only=True)
class TrackedItem:
    """Persisted product tracking aggregate."""

    item_id: str
    user_id: str = "legacy_owner"
    source_url: str
    canonical_url: str | None
    product_id: str | None
    site_key: str | None
    adapter_key: str | None
    product_name: str | None
    brand: str | None
    model: str | None
    sku: str | None
    gtin: str | None
    image_url: str | None
    availability: str | None
    currency: str | None
    last_amount: str | None
    last_raw_text: str | None
    last_source: str | None
    status: TrackingStatus
    check_interval_minutes: int
    target_amount: str | None
    created_at: str
    updated_at: str
    last_checked_at: str | None
    created_via: TrackingCreatedVia | None = None
    notify_on_price_increase: bool = False
    notify_on_stock_change: bool = False
    next_check_at: str | None = None
    lease_owner: str | None = None
    lease_until: str | None = None
    last_check_started_at: str | None = None
    last_check_finished_at: str | None = None

    @property
    def tracking_mode(self) -> TrackingMode:
        """`monitor` when no target is set, `alert` when target is set."""
        return derive_tracking_mode(self.target_amount)

    @property
    def alerts_enabled(self) -> bool:
        """Convenience flag for clients that only need a boolean."""
        return self.tracking_mode == "alert"


@dataclass(frozen=True)
class PriceSnapshot:
    """Historical snapshot of one tracking check execution."""

    snapshot_id: str
    item_id: str
    checked_at: str
    success: bool
    amount: str | None
    currency: str | None
    raw_text: str | None
    source: str | None
    error: str | None
    availability: str | None = None
    scheduled_for: str | None = None

@dataclass(frozen=True)
class TrackingCheckRun:
    """Auditable record of one scheduled tracking check attempt."""

    run_id: str
    item_id: str
    scheduled_for: str
    started_at: str
    finished_at: str | None
    status: TrackingCheckRunStatus
    attempt: int
    error: str | None
    job_key: str | None = None


@dataclass(frozen=True)
class TrackingBootstrapIdempotencyRecord:
    """Stored idempotency result for tracked-item bootstrap requests."""

    idempotency_key: str
    request_hash: str
    item_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AlertEvent:
    """Idempotent alert decision emitted from a successful snapshot."""

    event_id: str
    idempotency_key: str
    item_id: str
    snapshot_id: str
    rule_type: AlertRuleType
    previous_amount: str | None
    current_amount: str
    target_amount: str
    currency: str | None
    triggered_at: str


@dataclass(frozen=True)
class NotificationEvent:
    """Outbox row used by notifier workers for reliable alert delivery."""

    notification_id: str
    idempotency_key: str
    alert_event_id: str | None
    item_id: str
    channel: str
    payload: str
    status: NotificationEventStatus
    attempt_count: int
    max_attempts: int
    next_attempt_at: str
    last_attempt_at: str | None
    sent_at: str | None
    provider_name: str | None
    provider_message_id: str | None
    last_error: str | None
    lease_owner: str | None
    lease_until: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AffiliateClickEvent:
    """Minimal outbound click audit event."""

    event_id: str
    item_id: str | None
    user_id: str | None
    surface: str
    merchant_domain: str | None
    rule_id: str | None
    is_affiliate: bool
    created_at: str


@dataclass(frozen=True, kw_only=True)
class TelegramConnectSession:
    """Short-lived Telegram connection handshake state."""

    user_id: str = "legacy_owner"
    state: str
    status: TelegramConnectSessionStatus
    created_at: str
    expires_at: str
    connected_at: str | None
    telegram_user_id: str | None
    telegram_chat_id: str | None
    telegram_username: str | None


@dataclass(frozen=True)
class AppUser:
    """Application-owned user row mapped to external auth subject."""

    user_id: str
    auth_provider: AuthProvider
    auth_subject: str
    email: str | None
    display_name: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserEntitlement:
    """Resolved effective plan limits for one app user."""

    user_id: str
    plan_key: str
    max_active_trackers: int
    min_check_interval_minutes: int
    monthly_check_units: int
    allowed_channels: str
    overage_policy: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UsageCounter:
    """Per-user consumed check units for one billing period."""

    counter_id: str
    user_id: str
    period_start: str
    period_end: str
    consumed_check_units: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BillingSubscription:
    """Current billing provider state for one app user."""

    subscription_id: str
    user_id: str
    provider: str
    provider_customer_id: str | None
    provider_subscription_id: str | None
    provider_price_id: str | None
    plan_key: str | None
    status: str
    current_period_start: str | None
    current_period_end: str | None
    next_billed_at: str | None
    cancel_at_period_end: bool
    last_event_id: str | None
    last_event_occurred_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BillingSubscriptionApplyResult:
    """Result of applying a billing event to the subscription store."""

    processed: bool
    reason: str | None
    subscription: BillingSubscription | None
