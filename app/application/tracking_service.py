from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchService
from app.application.tracking_price_result import (
    normalize_tracking_price_result,
    resolve_listing_url,
)
from app.scraper import PriceResult
from app.tracking import (
    NotificationEvent,
    PriceSnapshot,
    TrackedItem,
    TrackingRepository,
    TrackingStatus,
)

_LEGACY_USER_ID = "legacy_owner"
_TEST_INTERVAL_15_SECONDS_SENTINEL = 10_079
_TEST_INTERVAL_15_SECONDS_VALUE = 15
_DEFAULT_NOTIFICATION_MAX_ATTEMPTS = 5
_NOT_IN_STOCK_AVAILABILITIES = {"out_of_stock", "preorder", "discontinued"}


class TrackingServiceError(RuntimeError):
    """Base tracking service error."""


class TrackingBootstrapError(TrackingServiceError):
    """Raised when URL bootstrap cannot produce a tracked item."""


class TrackingDuplicateError(TrackingServiceError):
    """Raised when caller opts out of refreshing an existing tracked item."""

    def __init__(self, item: TrackedItem) -> None:
        super().__init__("Already tracking this product.")
        self.item = item


@dataclass(frozen=True)
class TrackingBootstrapInput:
    """Input needed to create or refresh a tracked item from a single product URL."""

    request_id: str
    url: str
    timeout_seconds: int
    selected_sites: set[str] | None
    fetch_mode: str
    check_interval_minutes: int
    target_amount: str | None
    notify_on_price_increase: bool | None = None
    notify_on_stock_change: bool | None = None
    duplicate_policy: str = "upsert"
    created_via: str | None = None
    scrape_result_ref: str | None = None
    selected_listing_id: str | None = None
    pre_fetched_result: PriceResult | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class TrackingUpdateInput:
    """Partial update payload for mutable tracking settings."""

    item_id: str
    status: TrackingStatus | None
    check_interval_minutes: int | None
    target_amount: str | None
    target_amount_provided: bool
    notify_on_price_increase: bool | None = None
    notify_on_price_increase_provided: bool = False
    notify_on_stock_change: bool | None = None
    notify_on_stock_change_provided: bool = False
    user_id: str | None = None


@dataclass(frozen=True)
class TrackingDetailOutput:
    """Tracking item with a bounded recent snapshot history."""

    item: TrackedItem
    recent_snapshots: list[PriceSnapshot]


class TrackingService:
    """Tracks products and stores historical price snapshots.

    Lifecycle:
    1. Bootstrap scrapes one URL and upserts one tracked item.
    2. Each successful bootstrap appends an immutable snapshot.
    3. Update/list/detail/delete manage persisted tracking state.
    """

    def __init__(
        self,
        repository: TrackingRepository,
        scrape_service: ScrapeBatchService,
        default_interval_minutes: int,
        min_interval_minutes: int,
        max_interval_minutes: int,
        default_snapshot_limit: int,
    ) -> None:
        self._repository = repository
        self._scrape_service = scrape_service
        self._default_interval_minutes = default_interval_minutes
        self._min_interval_minutes = min_interval_minutes
        self._max_interval_minutes = max_interval_minutes
        self._default_snapshot_limit = default_snapshot_limit

    def list_sites(self) -> dict[str, str]:
        """Expose supported site keys via the existing scrape service."""
        return self._scrape_service.list_sites()

    def list_items(self, *, user_id: str | None = None) -> list[TrackedItem]:
        """Return tracked items, usually sorted by latest update."""
        return self._list_items_for_user(_resolve_user_id(user_id))

    def get_item(self, item_id: str, *, user_id: str | None = None) -> TrackedItem | None:
        """Return one tracked item or None when not found."""
        return self._get_item_for_user(item_id=item_id, user_id=_resolve_user_id(user_id))

    def get_item_by_source_url(
        self,
        source_url: str,
        *,
        user_id: str | None = None,
    ) -> TrackedItem | None:
        """Return one tracked item by exact source URL for one user."""
        return self._get_item_by_source_url_for_user(
            source_url=source_url,
            user_id=_resolve_user_id(user_id),
        )

    def get_detail(
        self,
        item_id: str,
        snapshot_limit: int | None = None,
        *,
        user_id: str | None = None,
    ) -> TrackingDetailOutput | None:
        """Return one tracked item with recent snapshots."""
        item = self._get_item_for_user(item_id=item_id, user_id=_resolve_user_id(user_id))
        if item is None:
            return None

        limit = snapshot_limit if snapshot_limit is not None else self._default_snapshot_limit
        if limit < 1:
            limit = 1

        snapshots = self._repository.list_snapshots(item_id=item.item_id, limit=limit)
        return TrackingDetailOutput(item=item, recent_snapshots=snapshots)

    def bootstrap(self, payload: TrackingBootstrapInput) -> TrackedItem:
        """Create/update tracked item from URL and persist first snapshot.

        Bootstrap is scrape-first by design: if we cannot extract a current
        price, the tracked item is not created.
        """
        user_id = _resolve_user_id(payload.user_id)
        self._require_active_telegram_chat(user_id=user_id)
        entitlement = self._resolve_entitlement(user_id=user_id)
        interval = self._normalize_interval(
            payload.check_interval_minutes,
            user_min_interval=entitlement.min_check_interval_minutes,
        )
        result = payload.pre_fetched_result
        if result is None:
            # Phase 1: run the same extractor pipeline used by /api/v1/scrapes.
            scrape_output = self._scrape_service.scrape(
                ScrapeBatchInput(
                    request_id=payload.request_id,
                    urls=[payload.url],
                    timeout_seconds=payload.timeout_seconds,
                    selected_sites=payload.selected_sites,
                    fetch_mode=payload.fetch_mode,
                )
            )
            if not scrape_output.results:
                raise TrackingBootstrapError("bootstrap failed: no_result")
            result = scrape_output.results[0]

        if not result.success:
            raise TrackingBootstrapError(
                f"bootstrap failed: {result.error or 'price_not_found'}"
            )

        # Phase 2: upsert one tracked item per source URL.
        checked_at = _utcnow_iso()
        source_url = result.url
        existing = self._get_item_by_source_url_for_user(source_url=source_url, user_id=user_id)
        if existing is not None and payload.duplicate_policy == "reject":
            raise TrackingDuplicateError(existing)

        explicit_listing_id = _normalize_optional(payload.selected_listing_id)
        tracked_listing_id = explicit_listing_id or (
            existing.tracked_listing_id if existing is not None else None
        )
        tracked_listing_url = existing.tracked_listing_url if existing is not None else None
        if explicit_listing_id:
            selected_listing = _find_listing_by_id(result, explicit_listing_id)
            if selected_listing is None:
                raise TrackingBootstrapError("selected_listing_not_found")
            tracked_listing_url = resolve_listing_url(
                selected_listing.listing_url,
                base_url=result.url,
            )
            if tracked_listing_url is None:
                raise TrackingBootstrapError("selected_listing_not_trackable")

        result = normalize_tracking_price_result(
            result,
            tracked_listing_id=tracked_listing_id,
            tracked_listing_url=tracked_listing_url,
            fallback_to_first=tracked_listing_id is None and tracked_listing_url is None,
        )
        if not result.success and result.error == "selected_listing_not_found":
            raise TrackingBootstrapError("selected_listing_not_found")
        normalized_availability = _normalize_availability(result.availability)
        is_not_in_stock = _is_not_in_stock(normalized_availability)
        if existing is None and not is_not_in_stock and not (result.amount or "").strip():
            raise TrackingBootstrapError("bootstrap failed: price_not_found")

        is_new_item = existing is None
        if existing is None:
            self._enforce_tracker_capacity(
                user_id=user_id,
                max_active=entitlement.max_active_trackers,
            )
        item_id = existing.item_id if existing is not None else _new_item_id()
        created_at = existing.created_at if existing is not None else checked_at

        target_amount = existing.target_amount if existing is not None else None
        if payload.target_amount is not None:
            target_amount = _normalize_target_amount(payload.target_amount)
        notify_on_price_increase = (
            existing.notify_on_price_increase if existing is not None else False
        )
        if payload.notify_on_price_increase is not None:
            notify_on_price_increase = bool(payload.notify_on_price_increase)
        notify_on_stock_change = (
            existing.notify_on_stock_change if existing is not None else False
        )
        if payload.notify_on_stock_change is not None:
            notify_on_stock_change = bool(payload.notify_on_stock_change)
        has_current_price = bool((result.amount or "").strip())
        should_update_price_fields = has_current_price and not is_not_in_stock

        status: TrackingStatus = existing.status if existing is not None else "active"
        created_via = existing.created_via if existing is not None else payload.created_via or "dashboard"

        item = TrackedItem(
            item_id=item_id,
            user_id=user_id,
            source_url=source_url,
            canonical_url=result.canonical_url or source_url,
            product_id=result.product_id,
            site_key=result.site_key,
            adapter_key=result.adapter_key,
            product_name=result.product_name,
            brand=result.brand,
            model=result.model,
            sku=result.sku,
            gtin=result.gtin,
            image_url=result.image_url,
            availability=normalized_availability,
            currency=(result.currency or (existing.currency if existing is not None else None)),
            last_amount=(
                result.amount
                if should_update_price_fields
                else (existing.last_amount if existing is not None else result.amount)
            ),
            last_raw_text=(
                result.raw_text
                if should_update_price_fields
                else (existing.last_raw_text if existing is not None else result.raw_text)
            ),
            last_source=(
                result.source
                if should_update_price_fields
                else (existing.last_source if existing is not None else result.source)
            ),
            created_via=created_via,
            status=status,
            check_interval_minutes=interval,
            target_amount=target_amount,
            notify_on_price_increase=notify_on_price_increase,
            notify_on_stock_change=notify_on_stock_change,
            created_at=created_at,
            updated_at=checked_at,
            last_checked_at=checked_at,
            next_check_at=_add_interval_iso(checked_at, interval),
            lease_owner=None,
            lease_until=None,
            last_check_started_at=None,
            last_check_finished_at=checked_at,
            tracked_listing_id=tracked_listing_id,
            tracked_listing_url=tracked_listing_url,
        )

        saved = self._repository.save_item(item)
        # Phase 3: append immutable history row for charts/audits.
        self._repository.add_snapshot(
            PriceSnapshot(
                snapshot_id=_new_snapshot_id(),
                item_id=saved.item_id,
                checked_at=checked_at,
                success=True,
                amount=(result.amount if should_update_price_fields else None),
                currency=(
                    (result.currency or saved.currency)
                    if should_update_price_fields
                    else None
                ),
                raw_text=(result.raw_text if should_update_price_fields else None),
                source=(result.source if should_update_price_fields else None),
                error=None,
                availability=saved.availability,
            )
        )
        if is_new_item and not is_not_in_stock:
            self._enqueue_tracking_started_notification(
                item=saved,
                result=result,
                triggered_at=checked_at,
            )
        return saved

    def bootstrap_idempotent(
        self,
        *,
        payload: TrackingBootstrapInput,
        idempotency_key: str,
    ) -> TrackedItem:
        """Bootstrap with idempotency semantics for safe client retries."""
        request_hash = _build_bootstrap_request_hash(payload)
        user_id = _resolve_user_id(payload.user_id)
        existing = self._get_bootstrap_idempotency_record(
            user_id=user_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise TrackingServiceError(
                    "Idempotency-Key reuse with different payload is not allowed"
                )
            if existing.item_id:
                item = self._get_item_for_user(item_id=existing.item_id, user_id=user_id)
                if item is not None:
                    return item

        item = self.bootstrap(replace(payload, user_id=user_id))
        self._save_bootstrap_idempotency_record(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            item_id=item.item_id,
            now=_utcnow_iso(),
        )
        return item

    def update_item(self, payload: TrackingUpdateInput) -> TrackedItem | None:
        """Patch mutable tracking config without executing a scrape."""
        user_id = _resolve_user_id(payload.user_id)
        entitlement = self._resolve_entitlement(user_id=user_id)

        current = self._get_item_for_user(item_id=payload.item_id, user_id=user_id)
        if current is None:
            return None

        interval = (
            self._normalize_interval(
                payload.check_interval_minutes,
                user_min_interval=entitlement.min_check_interval_minutes,
            )
            if payload.check_interval_minutes is not None
            else current.check_interval_minutes
        )

        target_amount = current.target_amount
        if payload.target_amount_provided:
            target_amount = _normalize_target_amount(payload.target_amount)
        notify_on_price_increase = current.notify_on_price_increase
        if payload.notify_on_price_increase_provided and payload.notify_on_price_increase is not None:
            notify_on_price_increase = bool(payload.notify_on_price_increase)
        notify_on_stock_change = current.notify_on_stock_change
        if payload.notify_on_stock_change_provided and payload.notify_on_stock_change is not None:
            notify_on_stock_change = bool(payload.notify_on_stock_change)

        updated_at = _utcnow_iso()
        next_status = payload.status or current.status
        updated = replace(
            current,
            status=next_status,
            check_interval_minutes=interval,
            target_amount=target_amount,
            notify_on_price_increase=notify_on_price_increase,
            notify_on_stock_change=notify_on_stock_change,
            updated_at=updated_at,
            next_check_at=(
                _add_interval_iso(updated_at, interval)
                if (payload.check_interval_minutes is not None or current.next_check_at is None)
                else current.next_check_at
            ),
        )
        saved = self._repository.save_item(updated)
        if current.status == "active" and saved.status == "paused":
            self._enqueue_tracking_stopped_notification(
                item=saved,
                triggered_at=updated_at,
            )
        return saved

    def delete_item(self, item_id: str, *, user_id: str | None = None) -> bool:
        """Delete tracked item and related snapshots (FK cascade)."""
        item = self._get_item_for_user(item_id=item_id, user_id=_resolve_user_id(user_id))
        if item is None:
            return False
        return self._repository.delete_item(item_id)

    def _normalize_interval(self, value: int | None, *, user_min_interval: int) -> int:
        """Apply default interval and enforce configured bounds."""
        interval = value if value is not None else self._default_interval_minutes
        floor = max(self._min_interval_minutes, user_min_interval)
        if interval < floor or interval > self._max_interval_minutes:
            raise TrackingServiceError(
                f"check_interval_minutes must be between {floor} and {self._max_interval_minutes}"
            )
        return interval

    def _enforce_tracker_capacity(self, *, user_id: str, max_active: int) -> None:
        if max_active <= 0:
            raise TrackingServiceError("Active tracker capacity exhausted")
        items = self._list_items_for_user(user_id)
        active_count = sum(1 for item in items if item.status == "active")
        if active_count >= max_active:
            raise TrackingServiceError(
                f"Active tracker limit reached ({max_active}) for current plan"
            )

    def _resolve_entitlement(self, *, user_id: str):
        if not hasattr(self._repository, "ensure_default_user_entitlement") or not hasattr(
            self._repository, "get_user_entitlement"
        ):
            return _FallbackEntitlement(min_check_interval_minutes=self._min_interval_minutes)
        try:
            self._repository.ensure_default_user_entitlement(user_id=user_id, now=_utcnow_iso())
            entitlement = self._repository.get_user_entitlement(user_id=user_id)
            if entitlement is not None:
                return entitlement
        except Exception as exc:  # pragma: no cover - fallback for legacy test doubles
            raise TrackingServiceError(f"missing entitlement: {exc}") from exc
        raise TrackingServiceError("missing entitlement")

    def _list_items_for_user(self, user_id: str) -> list[TrackedItem]:
        method = getattr(self._repository, "list_items_for_user", None)
        if callable(method):
            return method(user_id)
        return self._repository.list_items()

    def _get_item_for_user(self, *, item_id: str, user_id: str) -> TrackedItem | None:
        method = getattr(self._repository, "get_item_for_user", None)
        if callable(method):
            return method(item_id=item_id, user_id=user_id)
        return self._repository.get_item(item_id)

    def _get_item_by_source_url_for_user(
        self,
        *,
        source_url: str,
        user_id: str,
    ) -> TrackedItem | None:
        method = getattr(self._repository, "get_item_by_source_url_for_user", None)
        if callable(method):
            return method(source_url=source_url, user_id=user_id)
        return self._repository.get_item_by_source_url(source_url)

    def _get_bootstrap_idempotency_record(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ):
        method = getattr(self._repository, "get_bootstrap_idempotency_record", None)
        if method is None:
            return None
        try:
            return method(user_id=user_id, idempotency_key=idempotency_key)
        except TypeError:
            return method(idempotency_key=idempotency_key)

    def _save_bootstrap_idempotency_record(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        method = getattr(self._repository, "save_bootstrap_idempotency_record", None)
        if method is None:
            return
        try:
            method(
                user_id=user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                item_id=item_id,
                now=now,
            )
        except TypeError:
            method(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                item_id=item_id,
                now=now,
            )

    def _enqueue_tracking_started_notification(
        self,
        *,
        item: TrackedItem,
        result: PriceResult,
        triggered_at: str,
    ) -> None:
        enqueue_method = getattr(self._repository, "enqueue_notification_event", None)
        if not callable(enqueue_method):
            return

        active_chat_id = self._get_active_telegram_chat(user_id=item.user_id)
        if not active_chat_id:
            return

        product_url = (
            (result.canonical_url or "").strip()
            or (result.url or "").strip()
            or (item.canonical_url or "").strip()
            or (item.source_url or "").strip()
            or None
        )
        payload = {
            "notification_type": "tracking_started",
            "item_id": item.item_id,
            "tracking_mode": item.tracking_mode,
            "current_amount": item.last_amount,
            "target_amount": item.target_amount,
            "currency": item.currency,
            "triggered_at": triggered_at,
            "chat_id": active_chat_id,
        }
        if item.product_name:
            payload["product_name"] = item.product_name
        if product_url:
            payload["product_url"] = product_url
        if result.listings:
            listing_url = resolve_listing_url(
                result.listings[0].listing_url,
                base_url=result.url,
            )
            if listing_url:
                payload["listing_url"] = listing_url

        notification = NotificationEvent(
            notification_id=_new_notification_event_id(),
            idempotency_key=_build_notification_idempotency_key(
                event_key=f"tracking_started:{item.item_id}",
                channel="telegram",
            ),
            alert_event_id=None,
            item_id=item.item_id,
            channel="telegram",
            payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            status="queued",
            attempt_count=0,
            max_attempts=_DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
            next_attempt_at=triggered_at,
            last_attempt_at=None,
            sent_at=None,
            provider_name=None,
            provider_message_id=None,
            last_error=None,
            lease_owner=None,
            lease_until=None,
            created_at=triggered_at,
            updated_at=triggered_at,
        )
        enqueue_method(notification)

    def _enqueue_tracking_stopped_notification(
        self,
        *,
        item: TrackedItem,
        triggered_at: str,
    ) -> None:
        enqueue_method = getattr(self._repository, "enqueue_notification_event", None)
        if not callable(enqueue_method):
            return

        active_chat_id = self._get_active_telegram_chat(user_id=item.user_id)
        if not active_chat_id:
            return

        product_url = (item.canonical_url or "").strip() or (item.source_url or "").strip() or None
        payload = {
            "notification_type": "tracking_stopped",
            "item_id": item.item_id,
            "tracking_mode": item.tracking_mode,
            "current_amount": item.last_amount,
            "target_amount": item.target_amount,
            "currency": item.currency,
            "triggered_at": triggered_at,
            "chat_id": active_chat_id,
        }
        if item.product_name:
            payload["product_name"] = item.product_name
        if product_url:
            payload["product_url"] = product_url

        notification = NotificationEvent(
            notification_id=_new_notification_event_id(),
            idempotency_key=_build_notification_idempotency_key(
                event_key=f"tracking_stopped:{item.item_id}:{triggered_at}",
                channel="telegram",
            ),
            alert_event_id=None,
            item_id=item.item_id,
            channel="telegram",
            payload=json.dumps(payload, separators=(",", ":"), sort_keys=True),
            status="queued",
            attempt_count=0,
            max_attempts=_DEFAULT_NOTIFICATION_MAX_ATTEMPTS,
            next_attempt_at=triggered_at,
            last_attempt_at=None,
            sent_at=None,
            provider_name=None,
            provider_message_id=None,
            last_error=None,
            lease_owner=None,
            lease_until=None,
            created_at=triggered_at,
            updated_at=triggered_at,
        )
        enqueue_method(notification)

    def _get_active_telegram_chat(self, *, user_id: str) -> str | None:
        method = getattr(self._repository, "get_active_telegram_chat", None)
        if method is None:
            return None
        try:
            return method(user_id=user_id)
        except TypeError:
            return method()

    def _require_active_telegram_chat(self, *, user_id: str) -> None:
        active_chat_id = self._get_active_telegram_chat(user_id=user_id)
        if active_chat_id:
            return
        raise TrackingBootstrapError(
            "Connect Telegram in Settings before creating trackers."
        )


@dataclass(frozen=True)
class _FallbackEntitlement:
    min_check_interval_minutes: int
    max_active_trackers: int = 1_000_000


def _utcnow_iso() -> str:
    """Return current UTC timestamp in compact ISO-8601 form."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _resolve_user_id(user_id: str | None) -> str:
    normalized = (user_id or "").strip()
    return normalized or _LEGACY_USER_ID


def _new_item_id() -> str:
    """Generate opaque tracked-item IDs for external API clients."""
    return f"trk_{uuid.uuid4().hex[:20]}"


def _new_snapshot_id() -> str:
    """Generate opaque snapshot IDs."""
    return f"snp_{uuid.uuid4().hex[:20]}"


def _new_notification_event_id() -> str:
    return f"nev_{uuid.uuid4().hex[:20]}"


def _normalize_target_amount(value: str | None) -> str | None:
    """Treat blank targets as unset and trim user-provided strings."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None

def _add_interval_iso(timestamp: str, interval_value: int) -> str:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (dt + _interval_value_to_timedelta(interval_value)).isoformat().replace("+00:00", "Z")


def _interval_value_to_timedelta(interval_value: int) -> timedelta:
    if interval_value == _TEST_INTERVAL_15_SECONDS_SENTINEL:
        return timedelta(seconds=_TEST_INTERVAL_15_SECONDS_VALUE)
    return timedelta(minutes=interval_value)


def _build_bootstrap_request_hash(payload: TrackingBootstrapInput) -> str:
    selected_sites = (
        sorted(payload.selected_sites)
        if payload.selected_sites is not None
        else None
    )
    normalized = {
        "url": payload.url,
        "scrape_result_ref": payload.scrape_result_ref,
        "selected_listing_id": _normalize_optional(payload.selected_listing_id),
        "timeout_seconds": payload.timeout_seconds,
        "selected_sites": selected_sites,
        "fetch_mode": payload.fetch_mode,
        "check_interval_minutes": payload.check_interval_minutes,
        "target_amount": _normalize_target_amount(payload.target_amount),
        "notify_on_price_increase": payload.notify_on_price_increase,
        "notify_on_stock_change": payload.notify_on_stock_change,
        "duplicate_policy": payload.duplicate_policy,
        "created_via": payload.created_via or "dashboard",
    }
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_optional(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _find_listing_by_id(result: PriceResult, listing_id: str):
    for listing in result.listings:
        if listing.item_id == listing_id:
            return listing
    return None


def _build_notification_idempotency_key(*, event_key: str, channel: str) -> str:
    return f"{event_key}:{channel}"


def _normalize_availability(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def _is_not_in_stock(availability: str | None) -> bool:
    return _normalize_availability(availability) in _NOT_IN_STOCK_AVAILABILITIES
