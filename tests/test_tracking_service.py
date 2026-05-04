from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import pytest

from app.application.scrape_service import ScrapeBatchOutput
from app.application.tracking_service import (
    TrackingBootstrapError,
    TrackingBootstrapInput,
    TrackingDuplicateError,
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
    calls: int = 0

    def list_sites(self) -> dict[str, str]:
        return {"generic": "Generic extractor"}

    def scrape(self, payload) -> ScrapeBatchOutput:
        self.calls += 1
        return ScrapeBatchOutput(
            request_id=payload.request_id,
            total_urls=1,
            success_count=1 if self.result.success else 0,
            failure_count=0 if self.result.success else 1,
            results=[self.result],
        )


def _build_service(
    result: PriceResult,
    *,
    telegram_connected: bool = True,
) -> tuple[TrackingService, _FakeRepository, _FakeScrapeService]:
    chats: dict[str, str] = {}
    if telegram_connected:
        chats["legacy_owner"] = "12345"
    repository = _FakeRepository(
        items={},
        snapshots=[],
        idempotency_records={},
        notification_events=[],
        active_telegram_chat_by_user=chats,
    )
    scrape_service = _FakeScrapeService(result=result)
    service = TrackingService(
        repository=repository,
        scrape_service=scrape_service,
        default_interval_minutes=60,
        min_interval_minutes=5,
        max_interval_minutes=10080,
        default_snapshot_limit=20,
    )
    return service, repository, scrape_service


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


def _out_of_stock_result(url: str = "https://example.com/p/oos") -> PriceResult:
    return PriceResult(
        url=url,
        success=True,
        product_id="prd_test_oos",
        site_key="generic",
        adapter_key="generic",
        amount=None,
        currency=None,
        raw_text=None,
        source=None,
        product_name="Out Of Stock Product",
        image_url="https://example.com/oos.jpg",
        availability="out_of_stock",
        listing_count=0,
        listings=[],
    )


def test_bootstrap_creates_item_and_snapshot() -> None:
    service, repository, _ = _build_service(_success_result())

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
    assert item.notify_on_stock_change is False
    assert item.created_via == "dashboard"
    assert item.tracking_mode == "alert"
    assert item.alerts_enabled is True
    assert repository.list_snapshots(item.item_id, limit=10)


def test_bootstrap_interval_under_sixty_is_treated_as_minutes() -> None:
    service, _, _ = _build_service(_success_result("https://example.com/p/minutes"))

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
    service, _, _ = _build_service(_success_result("https://example.com/p/fifteen-seconds"))

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
    service, _, _ = _build_service(_success_result())

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
    service, _, _ = _build_service(_success_result("https://example.com/p/price-increase"))

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


def test_bootstrap_can_enable_stock_change_notifications() -> None:
    service, _, _ = _build_service(_success_result("https://example.com/p/stock-change"))

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_monitor_stock_change",
            url="https://example.com/p/stock-change",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
            notify_on_stock_change=True,
        )
    )

    assert item.notify_on_stock_change is True


def test_bootstrap_out_of_stock_keeps_snapshot_without_price_and_skips_started_notification() -> None:
    service, repository, _ = _build_service(_out_of_stock_result())

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_out_of_stock",
            url="https://example.com/p/oos",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    snapshots = repository.list_snapshots(item.item_id, limit=10)
    assert len(snapshots) == 1
    assert snapshots[0].success is True
    assert snapshots[0].amount is None
    assert snapshots[0].availability == "out_of_stock"
    assert item.last_amount is None
    assert item.availability == "out_of_stock"
    assert repository.notification_events == []


def test_bootstrap_can_create_extension_origin_item() -> None:
    service, _, _ = _build_service(_success_result("https://example.com/p/from-extension"))

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_extension_origin",
            url="https://example.com/p/from-extension",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
            created_via="extension",
        )
    )

    assert item.created_via == "extension"


def test_bootstrap_preserves_existing_created_via_on_upsert() -> None:
    service, repository, _ = _build_service(_success_result("https://example.com/p/preserve-origin"))

    first = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_first_origin",
            url="https://example.com/p/preserve-origin",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
            created_via="extension",
        )
    )
    second = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_second_origin",
            url="https://example.com/p/preserve-origin",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=120,
            target_amount=None,
            created_via="dashboard",
        )
    )

    assert first.item_id == second.item_id
    assert second.created_via == "extension"
    assert repository.items[first.item_id].created_via == "extension"


def test_bootstrap_preserves_legacy_null_created_via_on_upsert() -> None:
    service, repository, _ = _build_service(_success_result("https://example.com/p/legacy-origin"))

    first = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_first_legacy_origin",
            url="https://example.com/p/legacy-origin",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
            created_via="extension",
        )
    )
    repository.items[first.item_id] = TrackedItem(
        **{
            **first.__dict__,
            "created_via": None,
        }
    )

    second = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_second_legacy_origin",
            url="https://example.com/p/legacy-origin",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=120,
            target_amount=None,
            created_via="extension",
        )
    )

    assert second.item_id == first.item_id
    assert second.created_via is None


def test_bootstrap_new_item_enqueues_tracking_started_notification_when_chat_connected() -> None:
    service, repository, _ = _build_service(_success_result())

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


def test_bootstrap_uses_first_listing_for_saved_price_snapshot_and_notification() -> None:
    first_listing = PriceListing(
        item_id="itm_first",
        amount="90.00",
        currency="USD",
        raw_text="$90.00",
        source="test:first",
        seller="First Seller",
        listing_url="https://merchant.example.com/first",
    )
    later_listing = PriceListing(
        item_id="itm_later",
        amount="120.00",
        currency="USD",
        raw_text="$120.00",
        source="test:later",
        seller="Later Seller",
        listing_url="https://merchant.example.com/later",
    )
    result = PriceResult(
        url="https://example.com/p/multi",
        success=True,
        product_id="prd_multi",
        site_key="generic",
        adapter_key="generic",
        amount=later_listing.amount,
        currency=later_listing.currency,
        raw_text=later_listing.raw_text,
        source=later_listing.source,
        product_name="Multi Listing Product",
        listing_count=2,
        listings=[first_listing, later_listing],
    )
    service, repository, _ = _build_service(result)

    item = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_multi_listing",
            url=result.url,
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
        )
    )

    assert item.last_amount == "90.00"
    assert item.last_raw_text == "$90.00"
    assert item.last_source == "test:first"
    assert item.tracked_listing_id == "itm_first"
    assert item.tracked_listing_url == "https://merchant.example.com/first"
    assert repository.snapshots[-1].amount == "90.00"
    assert repository.snapshots[-1].raw_text == "$90.00"
    payload = json.loads(repository.notification_events[0].payload)
    assert payload["current_amount"] == "90.00"
    assert payload["listing_url"] == "https://merchant.example.com/first"


def test_bootstrap_existing_item_does_not_enqueue_tracking_started_notification() -> None:
    service, repository, _ = _build_service(_success_result("https://example.com/p/existing"))

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


def test_bootstrap_duplicate_policy_reject_returns_existing_without_mutation() -> None:
    service, repository, scrape_service = _build_service(
        _success_result("https://example.com/p/reject-existing")
    )

    first = service.bootstrap(
        TrackingBootstrapInput(
            request_id="req_first_reject",
            url="https://example.com/p/reject-existing",
            timeout_seconds=20,
            selected_sites=None,
            fetch_mode="auto",
            check_interval_minutes=60,
            target_amount=None,
            created_via="extension",
        )
    )
    snapshots_after_first = len(repository.snapshots)

    with pytest.raises(TrackingDuplicateError) as exc_info:
        service.bootstrap(
            TrackingBootstrapInput(
                request_id="req_second_reject",
                url="https://example.com/p/reject-existing",
                timeout_seconds=20,
                selected_sites=None,
                fetch_mode="auto",
                check_interval_minutes=120,
                target_amount="8.00",
                duplicate_policy="reject",
                created_via="dashboard",
            )
        )

    assert exc_info.value.item.item_id == first.item_id
    assert exc_info.value.item.created_via == "extension"
    assert len(repository.snapshots) == snapshots_after_first
    assert repository.items[first.item_id].target_amount is None
    assert repository.items[first.item_id].check_interval_minutes == 60
    assert repository.items[first.item_id].created_via == "extension"
    assert scrape_service.calls == 2


def test_bootstrap_rejects_failed_scrape() -> None:
    failed = PriceResult(url="https://example.com/p/1", success=False, error="price_not_found")
    service, _, _ = _build_service(failed)

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
    service, _, _ = _build_service(_success_result())
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
            notify_on_stock_change=True,
            notify_on_stock_change_provided=True,
        )
    )

    assert updated is not None
    assert updated.status == "paused"
    assert updated.check_interval_minutes == 120
    assert updated.target_amount == "8.50"
    assert updated.notify_on_price_increase is True
    assert updated.notify_on_stock_change is True
    assert updated.tracking_mode == "alert"


def test_update_item_can_clear_target_and_switch_to_monitor_mode() -> None:
    service, _, _ = _build_service(_success_result())
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
    service, repository, _ = _build_service(_success_result("https://example.com/p/paused-notify"))
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
    service, _, _ = _build_service(_success_result())
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
    service, repository, _ = _build_service(_success_result("https://example.com/p/idempo"))
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
    service, _, _ = _build_service(_success_result("https://example.com/p/mismatch"))
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


def test_bootstrap_requires_connected_telegram_before_scrape() -> None:
    service, _, scrape_service = _build_service(
        _success_result("https://example.com/p/requires-telegram"),
        telegram_connected=False,
    )

    with pytest.raises(
        TrackingBootstrapError,
        match="Connect Telegram in Settings before creating trackers",
    ):
        service.bootstrap(
            TrackingBootstrapInput(
                request_id="req_requires_telegram",
                url="https://example.com/p/requires-telegram",
                timeout_seconds=20,
                selected_sites=None,
                fetch_mode="auto",
                check_interval_minutes=60,
                target_amount=None,
            )
        )

    assert scrape_service.calls == 0
