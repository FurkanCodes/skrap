from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import pytest

from app.application.scrape_service import ScrapeBatchOutput
from app.application.tracking_service import (
    TrackingBootstrapError,
    TrackingBootstrapInput,
    TrackingService,
    TrackingServiceError,
    TrackingUpdateInput,
)
from app.scraper import PriceListing, PriceResult
from app.tracking import (
    NotificationEvent,
    PriceSnapshot,
    TrackedItem,
    TrackingBootstrapIdempotencyRecord,
)


@dataclass
class _FakeRepository:
    items: dict[str, TrackedItem]
    snapshots: list[PriceSnapshot]
    idempotency_records: dict[str, TrackingBootstrapIdempotencyRecord]
    notification_events: list[NotificationEvent]
    active_telegram_chat_by_user: dict[str, str]

    def initialize(self) -> None:
        pass

    def list_items(self) -> list[TrackedItem]:
        return list(self.items.values())

    def get_item(self, item_id: str) -> TrackedItem | None:
        return self.items.get(item_id)

    def get_item_by_source_url(self, source_url: str) -> TrackedItem | None:
        for item in self.items.values():
            if item.source_url == source_url:
                return item
        return None

    def save_item(self, item: TrackedItem) -> TrackedItem:
        self.items[item.item_id] = item
        return item

    def delete_item(self, item_id: str) -> bool:
        existed = item_id in self.items
        self.items.pop(item_id, None)
        return existed

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.snapshots.append(snapshot)

    def list_snapshots(self, item_id: str, limit: int) -> list[PriceSnapshot]:
        snapshots = [snapshot for snapshot in self.snapshots if snapshot.item_id == item_id]
        return snapshots[:limit]

    def get_bootstrap_idempotency_record(
        self,
        idempotency_key: str,
    ) -> TrackingBootstrapIdempotencyRecord | None:
        return self.idempotency_records.get(idempotency_key)

    def save_bootstrap_idempotency_record(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        item_id: str,
        now: str,
    ) -> None:
        self.idempotency_records[idempotency_key] = TrackingBootstrapIdempotencyRecord(
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            item_id=item_id,
            created_at=now,
            updated_at=now,
        )

    def enqueue_notification_event(self, event: NotificationEvent) -> bool:
        self.notification_events.append(event)
        return True

    def get_active_telegram_chat(self, *, user_id: str) -> str | None:
        return self.active_telegram_chat_by_user.get(user_id)


@dataclass
class _FakeScrapeService:
    result: PriceResult

    def list_sites(self) -> dict[str, str]:
        return {"generic": "Generic extractor"}

    def scrape(self, payload) -> ScrapeBatchOutput:
        return ScrapeBatchOutput(
            request_id=payload.request_id,
            total_urls=1,
            success_count=1 if self.result.success else 0,
            failure_count=0 if self.result.success else 1,
            results=[self.result],
        )


def _build_service(result: PriceResult) -> tuple[TrackingService, _FakeRepository]:
    repository = _FakeRepository(
        items={},
        snapshots=[],
        idempotency_records={},
        notification_events=[],
        active_telegram_chat_by_user={},
    )
    service = TrackingService(
        repository=repository,
        scrape_service=_FakeScrapeService(result=result),
        default_interval_minutes=60,
        min_interval_minutes=5,
        max_interval_minutes=10080,
        default_snapshot_limit=20,
    )
    return service, repository


def _success_result(url: str = "https://example.com/p/1") -> PriceResult:
    listing = PriceListing(
        item_id="itm_test",
        amount="10.00",
        currency="USD",
        raw_text="$10.00",
        source="test:fake",
    )
    return PriceResult(
        url=url,
        success=True,
        product_id="prd_test",
        site_key="generic",
        adapter_key="generic",
        amount=listing.amount,
        currency=listing.currency,
        raw_text=listing.raw_text,
        source=listing.source,
        product_name="Test Product",
        listing_count=1,
        listings=[listing],
    )


def test_bootstrap_creates_item_and_snapshot() -> None:
    service, repository = _build_service(_success_result())

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_1",
            url="https://example.com/p/1",
            timeout_seconds=20,
            selected_sites={"generic"},
            fetch_mode="auto",
            check_interval_minutes=45,
            target_amount="9.00",
        )
    )

    assert item.item_id.startswith("trk_")
    assert item.check_interval_minutes == 45
    assert item.target_amount == "9.00"
    assert item.notify_on_price_increase is False
    assert item.tracking_mode == "alert"
    assert item.alerts_enabled is True
    assert repository.list_snapshots(item.item_id, limit=10)


def test_bootstrap_interval_under_sixty_is_treated_as_minutes() -> None:
    service, _ = _build_service(_success_result("https://example.com/p/minutes"))

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_minutes",
            url="https://example.com/p/minutes",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=15,
            target_amount=None,
        )
    )

    assert item.next_check_at is not None
    checked_dt = datetime.fromisoformat(item.last_checked_at.replace("Z", "+00:00"))
    next_dt = datetime.fromisoformat(item.next_check_at.replace("Z", "+00:00"))
    assert (next_dt - checked_dt).total_seconds() == 900


def test_bootstrap_test_interval_schedules_fifteen_seconds() -> None:
    service, _ = _build_service(_success_result("https://example.com/p/fifteen-seconds"))

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_fifteen_seconds",
            url="https://example.com/p/fifteen-seconds",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=10_079,
            target_amount=None,
        )
    )

    assert item.next_check_at is not None
    checked_dt = datetime.fromisoformat(item.last_checked_at.replace("Z", "+00:00"))
    next_dt = datetime.fromisoformat(item.next_check_at.replace("Z", "+00:00"))
    assert (next_dt - checked_dt).total_seconds() == 15


def test_bootstrap_without_target_starts_monitor_mode() -> None:
    service, _ = _build_service(_success_result())

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_monitor",
            url="https://example.com/p/no-target",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    assert item.target_amount is None
    assert item.notify_on_price_increase is False
    assert item.tracking_mode == "monitor"
    assert item.alerts_enabled is False


def test_bootstrap_can_enable_price_increase_notifications() -> None:
    service, _ = _build_service(_success_result("https://example.com/p/price-increase"))

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_monitor_increase",
            url="https://example.com/p/price-increase",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
            notify_on_price_increase=True,
        )
    )

    assert item.notify_on_price_increase is True


def test_bootstrap_new_item_enqueues_tracking_started_notification_when_chat_connected() -> None:
    service, repository = _build_service(_success_result())
    repository.active_telegram_chat_by_user["legacy_owner"] = "12345"

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_tracking_started",
            url="https://example.com/p/start",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    assert len(repository.notification_events) == 1
    notification = repository.notification_events[0]
    assert notification.channel == "telegram"
    payload = json.loads(notification.payload)
    assert payload["notification_type"] == "tracking_started"
    assert payload["item_id"] == item.item_id
    assert payload["chat_id"] == "12345"
    assert payload["tracking_mode"] == "monitor"


def test_bootstrap_existing_item_does_not_enqueue_tracking_started_notification() -> None:
    service, repository = _build_service(_success_result("https://example.com/p/existing"))
    repository.active_telegram_chat_by_user["legacy_owner"] = "12345"

    first = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_first",
            url="https://example.com/p/existing",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )
    second = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_second",
            url="https://example.com/p/existing",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    assert first.item_id == second.item_id
    assert len(repository.notification_events) == 1


def test_bootstrap_rejects_failed_scrape() -> None:
    failed = PriceResult(url="https://example.com/p/1", success=False, error="price_not_found")
    service, _ = _build_service(failed)

    with pytest.raises(TrackingBootstrapError, match="price_not_found"):
        service.bootstrap(
            TrackingBootstrapInput(
                request_id="req_2",
                url="https://example.com/p/1",
                timeout_seconds=20,
                selected_sites=None,
                fetch_mode="auto",
                check_interval_minutes=60,
                target_amount=None,
            )
        )


def test_update_item_applies_patch_fields() -> None:
    service, _ = _build_service(_success_result())
    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_3",
            url="https://example.com/p/2",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    updated = service.update_item(
        TrackingUpdateInput(
            item_id=item.item_id,
            status="paused",
            check_interval_minutes=120,
            target_amount="8.50",
            target_amount_provided=True,
            notify_on_price_increase=True,
            notify_on_price_increase_provided=True,
        )
    )

    assert updated is not None
    assert updated.status == "paused"
    assert updated.check_interval_minutes == 120
    assert updated.target_amount == "8.50"
    assert updated.notify_on_price_increase is True
    assert updated.tracking_mode == "alert"


def test_update_item_can_clear_target_and_switch_to_monitor_mode() -> None:
    service, _ = _build_service(_success_result())
    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_5",
            url="https://example.com/p/4",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount="11.00",
        )
    )

    updated = service.update_item(
        TrackingUpdateInput(
            item_id=item.item_id,
            status=None,
            check_interval_minutes=None,
            target_amount="   ",
            target_amount_provided=True,
            notify_on_price_increase=None,
            notify_on_price_increase_provided=False,
        )
    )

    assert updated is not None
    assert updated.target_amount is None
    assert updated.tracking_mode == "monitor"
    assert updated.alerts_enabled is False


def test_update_item_paused_transition_enqueues_tracking_stopped_notification() -> None:
    service, repository = _build_service(_success_result("https://example.com/p/paused-notify"))
    repository.active_telegram_chat_by_user["legacy_owner"] = "12345"
    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_paused_notify",
            url="https://example.com/p/paused-notify",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )
    assert len(repository.notification_events) == 1
    repository.notification_events.clear()

    updated = service.update_item(
        TrackingUpdateInput(
            item_id=item.item_id,
            status="paused",
            check_interval_minutes=None,
            target_amount=None,
            target_amount_provided=False,
        )
    )

    assert updated is not None
    assert updated.status == "paused"
    assert len(repository.notification_events) == 1
    notification = repository.notification_events[0]
    assert notification.channel == "telegram"
    payload = json.loads(notification.payload)
    assert payload["notification_type"] == "tracking_stopped"
    assert payload["item_id"] == item.item_id
    assert payload["chat_id"] == "12345"


def test_update_item_rejects_interval_outside_bounds() -> None:
    service, _ = _build_service(_success_result())
    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_4",
            url="https://example.com/p/3",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    with pytest.raises(TrackingServiceError, match="check_interval_minutes"):
        service.update_item(
            TrackingUpdateInput(
                item_id=item.item_id,
                status=None,
                check_interval_minutes=1,
                target_amount=None,
                target_amount_provided=False,
            )
        )


def test_bootstrap_idempotent_reuses_saved_item() -> None:
    service, repository = _build_service(_success_result("https://example.com/p/idempo"))
    payload = TrackingBootstrapInput(
        request_id="req_idempo_1",
        url="https://example.com/p/idempo",
        timeout_seconds=20,
        selected_sites={"generic"},
        fetch_mode="auto",
        check_interval_minutes=60,
        target_amount="9.00",
    )

    first = service.bootstrap_idempotent(payload=payload, idempotency_key="idem-1")
    snapshots_after_first = len(repository.snapshots)
    second = service.bootstrap_idempotent(payload=payload, idempotency_key="idem-1")

    assert first.item_id == second.item_id
    assert len(repository.snapshots) == snapshots_after_first


def test_bootstrap_idempotent_rejects_payload_mismatch() -> None:
    service, _ = _build_service(_success_result("https://example.com/p/mismatch"))
    base_payload = TrackingBootstrapInput(
        request_id="req_idempo_2",
        url="https://example.com/p/mismatch",
        timeout_seconds=20,
        selected_sites={"generic"},
        fetch_mode="auto",
        check_interval_minutes=60,
        target_amount=None,
    )
    changed_payload = TrackingBootstrapInput(
        request_id="req_idempo_3",
        url="https://example.com/p/mismatch",
        timeout_seconds=25,
        selected_sites={"generic"},
        fetch_mode="auto",
        check_interval_minutes=60,
        target_amount=None,
    )

    service.bootstrap_idempotent(payload=base_payload, idempotency_key="idem-2")
    with pytest.raises(
        TrackingServiceError,
        match="Idempotency-Key reuse with different payload",
    ):
        service.bootstrap_idempotent(payload=changed_payload, idempotency_key="idem-2")
