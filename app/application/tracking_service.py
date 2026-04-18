from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

from app.application.scrape_service import ScrapeBatchInput, ScrapeBatchService
from app.scraper import PriceResult
from app.tracking import PriceSnapshot, TrackedItem, TrackingRepository, TrackingStatus

_LEGACY_USER_ID = "legacy_owner"


class TrackingServiceError(RuntimeError):
    """Base tracking service error."""


class TrackingBootstrapError(TrackingServiceError):
    """Raised when URL bootstrap cannot produce a tracked item."""


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
    scrape_result_ref: str | None = None
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

        status: TrackingStatus = existing.status if existing is not None else "active"

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
            availability=result.availability,
            currency=result.currency,
            last_amount=result.amount,
            last_raw_text=result.raw_text,
            last_source=result.source,
            status=status,
            check_interval_minutes=interval,
            target_amount=target_amount,
            created_at=created_at,
            updated_at=checked_at,
            last_checked_at=checked_at,
            next_check_at=_add_minutes_iso(checked_at, interval),
            lease_owner=None,
            lease_until=None,
            last_check_started_at=None,
            last_check_finished_at=checked_at,
        )

        saved = self._repository.save_item(item)
        # Phase 3: append immutable history row for charts/audits.
        self._repository.add_snapshot(
            PriceSnapshot(
                snapshot_id=_new_snapshot_id(),
                item_id=saved.item_id,
                checked_at=checked_at,
                success=True,
                amount=result.amount,
                currency=result.currency,
                raw_text=result.raw_text,
                source=result.source,
                error=None,
            )
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

        updated_at = _utcnow_iso()
        updated = replace(
            current,
            status=payload.status or current.status,
            check_interval_minutes=interval,
            target_amount=target_amount,
            updated_at=updated_at,
            next_check_at=(
                _add_minutes_iso(updated_at, interval)
                if (payload.check_interval_minutes is not None or current.next_check_at is None)
                else current.next_check_at
            ),
        )
        return self._repository.save_item(updated)

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


def _normalize_target_amount(value: str | None) -> str | None:
    """Treat blank targets as unset and trim user-provided strings."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None

def _add_minutes_iso(timestamp: str, minutes: int) -> str:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (dt + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _build_bootstrap_request_hash(payload: TrackingBootstrapInput) -> str:
    selected_sites = (
        sorted(payload.selected_sites)
        if payload.selected_sites is not None
        else None
    )
    normalized = {
        "url": payload.url,
        "scrape_result_ref": payload.scrape_result_ref,
        "timeout_seconds": payload.timeout_seconds,
        "selected_sites": selected_sites,
        "fetch_mode": payload.fetch_mode,
        "check_interval_minutes": payload.check_interval_minutes,
        "target_amount": _normalize_target_amount(payload.target_amount),
    }
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
